"""
Coin alert bot: DexScreener -> Telegram.
Posts tokens that have a Telegram group, tagged NEWLY LAUNCHED and/or TRENDING (boosted).
Runs ONCE per invocation (designed for GitHub Actions). State lives in data/state.json.
"""
import html
import json
import logging
import os
import re
import time

import requests

API = "https://api.dexscreener.com"
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
TRENDING_TOP = int(os.getenv("TRENDING_TOP", "10"))
NEW_MAX_AGE_HOURS = float(os.getenv("NEW_MAX_AGE_HOURS", "48"))  # token must be this young to count as new
CHAINS = {c.strip().lower() for c in os.getenv("CHAINS", "").split(",") if c.strip()}  # empty = all
POST_ON_FIRST_RUN = os.getenv("POST_ON_FIRST_RUN", "0") == "1"
STATE_PATH = os.getenv("STATE_PATH", "data/state.json")
MAX_FETCH = int(os.getenv("MAX_FETCH", "30"))  # token lookups per run

RECHECK_NO_TG_HOURS = 48
RECHECK_INTERVAL_MIN = 25
TRENDING_COOLDOWN_HOURS = 24
API_DELAY = 0.4

NEW_TAG, TREND_TAG = "🆕 NEWLY LAUNCHED", "🔥 TRENDING (boosted)"
TG_RE = re.compile(r"^https?://(?:www\.)?(?:t|telegram)\.me/[^/\s]+", re.I)

log = logging.getLogger("bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
session = requests.Session()
session.headers.update({"User-Agent": "CoinAlertBot/2.0", "Accept": "application/json"})


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
    return {"chain": info["chain"], "address": info["address"], "name": info["name"],
            "symbol": info["symbol"], "tg": info["tg"], "first_seen": now,
            "last_checked": now, "posted_new": posted_new, "last_trending": 0}


# ---------- DexScreener ----------
def api(path):
    r = session.get(API + path, timeout=25)
    r.raise_for_status()
    time.sleep(API_DELAY)
    data = r.json()
    if isinstance(data, dict):
        data = data.get("data") or data.get("pairs") or []
    return data if isinstance(data, list) else []


def telegram_from_links(links):
    for link in links or []:
        url = (link or {}).get("url") or ""
        if TG_RE.match(url):
            return url.split("?")[0]
    return None


def pick(links, kind):
    for link in links or []:
        if (link.get("type") or "").lower() == kind and link.get("url"):
            return link["url"]
    return None


def fetch_token(chain, address, seed_links=None):
    """Market data + socials for one token. Returns dict or None on failure."""
    try:
        pairs = api(f"/tokens/v1/{chain}/{address}")
    except Exception as exc:
        log.warning("lookup %s/%s failed: %s", chain, address, exc)
        return None
    best = max(pairs, key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0), default=None)
    info = {"key": f"{chain}:{address}", "chain": chain, "address": address,
            "name": "", "symbol": "", "tg": telegram_from_links(seed_links),
            "x": pick(seed_links, "twitter"), "site": None, "price": None, "mcap": None,
            "liq": None, "vol24": None, "age_h": None,
            "url": f"https://dexscreener.com/{chain}/{address}"}
    if best:
        base = best.get("baseToken") or {}
        info["name"], info["symbol"] = base.get("name", ""), base.get("symbol", "")
        info["price"] = best.get("priceUsd")
        info["mcap"] = best.get("marketCap") or best.get("fdv")
        info["liq"] = (best.get("liquidity") or {}).get("usd")
        info["vol24"] = (best.get("volume") or {}).get("h24")
        info["url"] = best.get("url") or info["url"]
        created = best.get("pairCreatedAt")
        if created:
            info["age_h"] = max(0.0, (time.time() * 1000 - created) / 3_600_000)
        pinfo = best.get("info") or {}
        socials = pinfo.get("socials") or []
        info["tg"] = info["tg"] or telegram_from_links(socials)
        info["x"] = info["x"] or pick(socials, "twitter")
        sites = pinfo.get("websites") or []
        info["site"] = sites[0].get("url") if sites else None
    return info


# ---------- telegram ----------
def money(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    for lim, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= lim:
            return f"${v / lim:.2f}{suf}"
    return f"${v:,.0f}"


def age_text(h):
    if h is None:
        return None
    if h < 1:
        return f"{int(h * 60)}m"
    if h < 48:
        return f"{h:.0f}h"
    return f"{h / 24:.0f}d"


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
    sym = f" (${e(info['symbol'])})" if info["symbol"] else ""
    lines = [f"<b>{' + '.join(tags)}</b>", f"<b>{e(info['name'] or info['address'][:8])}</b>{sym}"]
    bits = [f"Chain: {e(info['chain'])}"]
    if age_text(info["age_h"]):
        bits.append(f"Age: {age_text(info['age_h'])}")
    lines.append(" | ".join(bits))
    stats = []
    if info["price"]:
        stats.append(f"Price: ${e(str(info['price']))}")
    for label, val in (("MCap", info["mcap"]), ("Liq", info["liq"]), ("Vol24h", info["vol24"])):
        if money(val):
            stats.append(f"{label}: {money(val)}")
    if stats:
        lines.append(" | ".join(stats))
    lines.append(f"CA: <code>{e(info['address'])}</code>")
    links = [f'<a href="{e(info["tg"])}">Telegram</a>']
    if info["x"]:
        links.append(f'<a href="{e(info["x"])}">X</a>')
    if info["site"]:
        links.append(f'<a href="{e(info["site"])}">Website</a>')
    links.append(f'<a href="{e(info["url"])}">DexScreener</a>')
    lines.append(" | ".join(links))
    lines.append("<i>Not financial advice. DYOR. 'Boosted' means paid promotion, not organic trending.</i>")
    return "\n".join(lines)


# ---------- main logic ----------
def chain_ok(chain):
    return not CHAINS or chain.lower() in CHAINS


def run_once(state):
    now = time.time()
    coins = state["coins"]
    first_run = not state.get("initialized")
    try:
        profiles = [p for p in api("/token-profiles/latest/v1") if chain_ok(p.get("chainId", ""))]
        boosts = [b for b in api("/token-boosts/top/v1") if chain_ok(b.get("chainId", ""))][:TRENDING_TOP]
    except Exception as exc:
        log.error("DexScreener list request failed: %s", exc)
        return False
    log.info("profiles=%d boosted=%d known=%d", len(profiles), len(boosts), len(coins))
    if not profiles and not boosts:
        log.error("nothing returned - API may have changed")
        return False

    fetches = [0]

    def lookup(chain, addr, links=None):
        if fetches[0] >= MAX_FETCH:
            return None
        fetches[0] += 1
        return fetch_token(chain, addr, links)

    seed_only = first_run and not POST_ON_FIRST_RUN
    pending = {}

    # 1) newly listed profiles
    for p in profiles:
        chain, addr = p.get("chainId"), p.get("tokenAddress")
        if not chain or not addr:
            continue
        key = f"{chain}:{addr}"
        if key in coins:
            continue
        if seed_only:
            coins[key] = {"chain": chain, "address": addr, "name": "", "symbol": "", "tg": None,
                          "first_seen": now, "last_checked": now, "posted_new": 1, "last_trending": 0}
            continue
        info = lookup(chain, addr, p.get("links"))
        if not info:
            continue  # not stored -> retried next run
        if info["age_h"] is None or info["age_h"] > NEW_MAX_AGE_HOURS:
            coins[key] = new_rec(info, now, 1)  # old token, never announce as new
        elif info["tg"]:
            pending[key] = {"tags": {NEW_TAG}, "info": info}
        else:
            coins[key] = new_rec(info, now, 0)

    # 2) young tokens with no Telegram yet: re-check for a while
    for key, c in list(coins.items()):
        if c.get("tg") or c.get("posted_new") or "chain" not in c:
            continue
        if c["first_seen"] < now - RECHECK_NO_TG_HOURS * 3600:
            continue
        if c["last_checked"] > now - RECHECK_INTERVAL_MIN * 60:
            continue
        info = lookup(c["chain"], c["address"])
        if not info:
            continue
        c["last_checked"] = now
        if info["tg"]:
            pending.setdefault(key, {"tags": set(), "info": info})["tags"].add(NEW_TAG)

    # 3) trending (boosted)
    for b in boosts:
        chain, addr = b.get("chainId"), b.get("tokenAddress")
        if not chain or not addr:
            continue
        key = f"{chain}:{addr}"
        c = coins.get(key)
        if c and now - c.get("last_trending", 0) < TRENDING_COOLDOWN_HOURS * 3600:
            continue
        info = pending[key]["info"] if key in pending else lookup(chain, addr, b.get("links"))
        if not info or not info["tg"]:
            continue
        pending.setdefault(key, {"tags": set(), "info": info})["tags"].add(TREND_TAG)

    # 4) send, then record
    for key, p in pending.items():
        tags = [t for t in (NEW_TAG, TREND_TAG) if t in p["tags"]]
        if not send(build_message(p["info"], tags)):
            continue
        info = p["info"]
        c = coins.setdefault(key, new_rec(info, now, 0))
        c["tg"], c["name"], c["symbol"] = info["tg"], info["name"], info["symbol"]
        c["posted_new"] = 1
        if TREND_TAG in tags:
            c["last_trending"] = now
        log.info("posted %s %s", key, tags)

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
