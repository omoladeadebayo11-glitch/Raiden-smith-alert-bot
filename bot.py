"""
Coin alert bot: watches CoinSniper and posts coins that have a Telegram group
to a Telegram channel/group, tagged NEWLY LAUNCHED and/or TRENDING.

Runs ONCE per invocation (designed for GitHub Actions). State lives in data/state.json.
"""
import html
import logging
import os
import re
import json
import time

import requests
from bs4 import BeautifulSoup

BASE = "https://coinsniper.net"
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "600"))
TRENDING_TOP = int(os.getenv("TRENDING_TOP", "10"))
# Promoted coins appear on every page; real new coins have the highest IDs.
NEW_ID_WINDOW = int(os.getenv("NEW_ID_WINDOW", "300"))
POST_ON_FIRST_RUN = os.getenv("POST_ON_FIRST_RUN", "0") == "1"
STATE_PATH = os.getenv("STATE_PATH", "data/state.json")
MAX_FETCH = int(os.getenv("MAX_FETCH", "25"))  # coin pages per run

RECHECK_NO_TG_HOURS = 48      # keep re-checking coins that had no Telegram yet
RECHECK_INTERVAL_MIN = 25
TRENDING_COOLDOWN_HOURS = 24  # don't repeat a trending post within this time
PAGE_DELAY = 2.0              # seconds between page requests (be polite)

log = logging.getLogger("bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; CoinAlertBot/1.0)",
    "Accept-Language": "en",
})

COIN_RE = re.compile(r"/coin/(\d+)")
TG_RE = re.compile(r"^https?://(?:www\.)?(?:t|telegram)\.me/", re.I)
EXPLORER_RE = re.compile(r"/(?:token|address|account)/([A-Za-z0-9_\-]{20,})")


# ---------- state ----------
def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)


def new_rec(info, now, posted_new):
    return {"name": info["name"], "symbol": info["symbol"], "tg": info["tg"],
            "first_seen": now, "last_checked": now,
            "posted_new": posted_new, "last_trending": 0}


# ---------- scraping ----------
def get(url):
    r = session.get(url, timeout=25)
    r.raise_for_status()
    time.sleep(PAGE_DELAY)
    return r.text


def ids_from_anchors(soup):
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        m = COIN_RE.search(a["href"])
        if m:
            cid = int(m.group(1))
            if cid not in seen:
                seen.add(cid)
                out.append(cid)
    return out


def parse_new_ids(page_html):
    ids = ids_from_anchors(BeautifulSoup(page_html, "html.parser"))
    if not ids:
        return []
    top = max(ids)
    return [i for i in ids if i >= top - NEW_ID_WINDOW]


def parse_trending_ids(page_html, limit):
    """Rank order from the first coin link in each table row."""
    soup = BeautifulSoup(page_html, "html.parser")
    out = []
    for tr in soup.select("table tr"):
        a = tr.find("a", href=COIN_RE)
        if a:
            cid = int(COIN_RE.search(a["href"]).group(1))
            if cid not in out:
                out.append(cid)
        if len(out) >= limit:
            break
    return out


def search(pattern, text):
    m = re.search(pattern, text, re.S | re.I)
    return m.group(1).strip() if m else None


def parse_coin(coin_id, page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    m = re.match(r"^(.*?)\s*\((.*?)\)\s*Price", title)
    name = m.group(1) if m else (soup.h1.get_text(strip=True) if soup.h1 else f"Coin {coin_id}")
    symbol = m.group(2) if m else ""

    tg = x = site = contract = chain = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        low = href.lower()
        if not tg and TG_RE.match(href) and "coinsnipernet" not in low:
            tg = href.split("?")[0]
        elif not x and re.match(r"^https?://(?:www\.)?(?:x|twitter)\.com/", low) and "coinsniper_net" not in low:
            x = href
        elif not site and a.get_text(strip=True).lower() in ("website", "visit website") and "coinsniper.net" not in low:
            site = href
        elif not contract and ("scan" in low or "explorer" in low):
            mm = EXPLORER_RE.search(href)
            if mm:
                contract = mm.group(1)
        elif not chain and "/chain/" in low:
            chain = a.get_text(strip=True) or None

    text = soup.get_text("\n", strip=True)
    flat = " ".join(text.split())
    info = {
        "id": coin_id, "name": name, "symbol": symbol, "tg": tg, "x": x,
        "site": site, "contract": contract, "chain": chain,
        "price": search(r"\bPrice\s*(\$[\d.,]+)", flat),
        "mcap": search(r"Market Cap\s*(\$[\d.,]+\w?)", flat),
        "holders": search(r"HOLDER COUNT.*?is\s*(\d[\d,]*\d|\d)", flat),
        "top10": search(r"TOP 10 HOLDER %.*?([\d.]+)%", flat),
    }
    return info


def fetch_coin(coin_id):
    return parse_coin(coin_id, get(f"{BASE}/coin/{coin_id}"))


# ---------- telegram ----------
def send(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    for _ in range(3):
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        if not r.ok:
            log.error("Telegram error %s: %s", r.status_code, r.text[:200])
            return False
        time.sleep(1.5)
        return True
    return False


def build_message(info, tags):
    e = html.escape
    header = " + ".join(tags)
    sym = f" (${e(info['symbol'])})" if info["symbol"] else ""
    lines = [f"<b>{header}</b>", f"<b>{e(info['name'])}</b>{sym}"]
    if info["chain"]:
        lines.append(f"Chain: {e(info['chain'])}")
    if info["price"]:
        lines.append(f"Price: {e(info['price'])}" + (f" | MCap: {e(info['mcap'])}" if info["mcap"] else ""))
    if info["contract"]:
        lines.append(f"CA: <code>{e(info['contract'])}</code>")
    links = [f'<a href="{e(info["tg"])}">Telegram</a>']
    if info["x"]:
        links.append(f'<a href="{e(info["x"])}">X</a>')
    if info["site"]:
        links.append(f'<a href="{e(info["site"])}">Website</a>')
    links.append(f'<a href="{BASE}/coin/{info["id"]}">CoinSniper</a>')
    lines.append(" | ".join(links))
    if info["holders"] or info["top10"]:
        bits = []
        if info["holders"]:
            bits.append(f"holders: {e(info['holders'])}")
        if info["top10"]:
            bits.append(f"top-10 hold: {e(info['top10'])}%")
        lines.append("Scan: " + ", ".join(bits))
    lines.append("<i>Not financial advice. DYOR. Listings and rankings can be paid or vote-driven.</i>")
    return "\n".join(lines)


# ---------- main logic ----------
NEW_TAG, TREND_TAG = "🆕 NEWLY LAUNCHED", "🔥 TRENDING"


def safe_fetch(coin_id):
    try:
        return fetch_coin(coin_id)
    except Exception as exc:
        log.warning("fetch %s failed: %s", coin_id, exc)
        return None


def run_once(state):
    now = time.time()
    coins = state["coins"]
    first_run = not state.get("initialized")
    try:
        new_ids = parse_new_ids(get(f"{BASE}/new"))
        trend_ids = parse_trending_ids(get(f"{BASE}/today"), TRENDING_TOP)
    except Exception as exc:
        log.error("list pages failed: %s", exc)
        return False
    log.info("new=%d trending=%d known=%d", len(new_ids), len(trend_ids), len(coins))
    if not new_ids:
        log.error("no coins parsed from /new - layout may have changed")
        return False

    fetches = [0]

    def fetch(cid):
        if fetches[0] >= MAX_FETCH:
            return None
        fetches[0] += 1
        return safe_fetch(cid)

    seed_only = first_run and not POST_ON_FIRST_RUN
    pending = {}  # key -> {"tags": set, "info": dict}

    # 1) brand-new coins
    for cid in new_ids:
        key = str(cid)
        if key in coins:
            continue
        if seed_only:
            coins[key] = {"name": "", "symbol": "", "tg": None, "first_seen": now,
                          "last_checked": now, "posted_new": 1, "last_trending": 0}
            continue
        info = fetch(cid)
        if not info:
            continue  # not stored, so it is retried next run
        if info["tg"]:
            pending[key] = {"tags": {NEW_TAG}, "info": info}
        else:
            coins[key] = new_rec(info, now, 0)

    # 2) coins that had no Telegram yet: re-check for a while
    for key, c in list(coins.items()):
        if c["tg"] or c["posted_new"]:
            continue
        if c["first_seen"] < now - RECHECK_NO_TG_HOURS * 3600:
            continue
        if c["last_checked"] > now - RECHECK_INTERVAL_MIN * 60:
            continue
        info = fetch(int(key))
        if not info:
            continue
        c["last_checked"] = now
        if info["tg"]:
            pending.setdefault(key, {"tags": set(), "info": info})["tags"].add(NEW_TAG)

    # 3) trending
    for cid in trend_ids:
        key = str(cid)
        c = coins.get(key)
        if c and now - c["last_trending"] < TRENDING_COOLDOWN_HOURS * 3600:
            continue
        info = pending[key]["info"] if key in pending else fetch(cid)
        if not info or not info["tg"]:
            continue
        pending.setdefault(key, {"tags": set(), "info": info})["tags"].add(TREND_TAG)

    # 4) send, then record
    for key, p in pending.items():
        tags = [t for t in (NEW_TAG, TREND_TAG) if t in p["tags"]]
        if not send(build_message(p["info"], tags)):
            continue  # not recorded -> retried next run
        c = coins.setdefault(key, new_rec(p["info"], now, 0))
        c["tg"], c["name"], c["symbol"] = p["info"]["tg"], p["info"]["name"], p["info"]["symbol"]
        if NEW_TAG in tags:
            c["posted_new"] = 1
        else:
            c["posted_new"] = c["posted_new"] or 1
        if TREND_TAG in tags:
            c["last_trending"] = now
        log.info("posted %s %s", key, tags)

    # prune very old entries to keep the file small
    for key in [k for k, c in coins.items() if c["first_seen"] < now - 30 * 86400]:
        del coins[key]
    state["initialized"] = True
    return True


def main():
    state = load_state() or {"coins": {}, "initialized": False}
    run_once(state)
    save_state(state)


if __name__ == "__main__":
    main()
