"""
Coin alert bot: GeckoTerminal + DexScreener -> Telegram.
Posts tokens that have a Telegram group, tagged:
  NEWLY LAUNCHED  (fresh pools from GeckoTerminal / new DexScreener profiles)
  TRENDING        (GeckoTerminal trending pools: real visits + on-chain activity)
  BOOSTED         (DexScreener boosts: paid promotion)
Runs ONCE per invocation (designed for GitHub Actions). State lives in data/state.json.
"""
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import requests

DEX = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2"
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
TRENDING_TOP = int(os.getenv("TRENDING_TOP", "10"))
GT_NEW_PAGES = int(os.getenv("GT_NEW_PAGES", "3"))            # 20 new pools per page
NEW_MAX_AGE_HOURS = float(os.getenv("NEW_MAX_AGE_HOURS", "48"))
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "0"))
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "15"))
MAX_LOOKUPS = int(os.getenv("MAX_LOOKUPS", "300"))
CHAINS = {c.strip().lower() for c in os.getenv("CHAINS", "").split(",") if c.strip()}  # empty = all
POST_ON_FIRST_RUN = os.getenv("POST_ON_FIRST_RUN", "0") == "1"
STATE_PATH = os.getenv("STATE_PATH", "data/state.json")
RUN_MINUTES = float(os.getenv("RUN_MINUTES", "0"))        # 0 = single check, then exit
INTERVAL_SECONDS = int(os.getenv("INTERVAL_SECONDS", "180"))  # pause between checks

RECHECK_NO_TG_HOURS = 24
RECHECK_INTERVAL_MIN = 10
COOLDOWN_HOURS = 24
DEX_DELAY, GT_DELAY = 0.3, 2.2   # GeckoTerminal free tier: 30 calls/min

NEW_TAG, TREND_TAG, BOOST_TAG = "🆕 NEWLY LAUNCHED", "🔥 TRENDING", "🚀 BOOSTED"
TG_RE = re.compile(r"^https?://(?:www\.)?(?:t|telegram)\.me/[^/\s]+", re.I)
GT_CHAIN = {"eth": "ethereum", "polygon_pos": "polygon", "avax": "avalanche",
            "sui-network": "sui", "bsc": "bsc"}
CHAIN_NAMES = {"bsc": "BNB Chain", "ethereum": "Ethereum", "solana": "Solana", "base": "Base",
               "arbitrum": "Arbitrum", "polygon": "Polygon", "avalanche": "Avalanche",
               "optimism": "Optimism", "sui": "Sui", "ton": "TON", "tron": "Tron"}

log = logging.getLogger("bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
session = requests.Session()
session.headers.update({"User-Agent": "CoinAlertBot/3.0", "Accept": "application/json"})


# ---------- helpers / state ----------
def norm(addr):
    return addr.lower() if addr.startswith("0x") else addr


def mk(chain, addr):
    return f"{chain}:{norm(addr)}"


def chain_ok(chain):
    return not CHAINS or chain.lower() in CHAINS


def load_state():
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    state["coins"] = {k.lower() if ":0x" in k else k: v for k, v in state.get("coins", {}).items()}
    return state


def save_state(state):
    """One coin per line, so git diffs stay small."""
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    rows = ",\n".join(f"{json.dumps(k)}:{json.dumps(v, separators=(',', ':'))}"
                      for k, v in sorted(state["coins"].items()))
    with open(STATE_PATH, "w") as f:
        f.write('{"initialized":%s,"coins":{\n%s\n}}\n' % (json.dumps(bool(state.get("initialized"))), rows))


def new_rec(info, now, posted_new):
    return {"tg": info["tg"], "first_seen": int(now), "last_checked": int(now),
            "posted_new": posted_new, "last_trending": 0}


# ---------- data sources ----------
def dex_get(path):
    r = session.get(DEX + path, timeout=25)
    r.raise_for_status()
    time.sleep(DEX_DELAY)
    data = r.json()
    if isinstance(data, dict):
        data = data.get("data") or data.get("pairs") or []
    return data if isinstance(data, list) else []


def gt_pools(path, params=None):
    """Returns [(chain, base_token_address, age_hours)] - or [] on any failure."""
    try:
        r = session.get(GT + path, params=params, timeout=25,
                        headers={"Accept": "application/json;version=20230302"})
        r.raise_for_status()
        time.sleep(GT_DELAY)
        pools = r.json().get("data", [])
    except Exception as exc:
        log.warning("GeckoTerminal %s failed: %s", path, exc)
        return []
    out = []
    for p in pools:
        tid = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
        if "_" not in tid:
            continue
        net, addr = tid.split("_", 1)
        chain = GT_CHAIN.get(net, net)
        age = None
        created = (p.get("attributes") or {}).get("pool_created_at")
        if created:
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
            except ValueError:
                pass
        if chain_ok(chain):
            out.append((chain, addr, age))
    return out


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


def info_from_pairs(chain, addr, pairs, links, age_hint):
    best = max(pairs, key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0), default=None)
    info = {"chain": chain, "address": addr, "name": "", "symbol": "",
            "tg": telegram_from_links(links), "mcap": None, "liq": None, "vol24": None,
            "age_h": age_hint}
    if best:
        base = best.get("baseToken") or {}
        info["address"] = base.get("address") or addr
        info["name"], info["symbol"] = base.get("name", ""), base.get("symbol", "")
        info["mcap"] = best.get("marketCap") or best.get("fdv")
        info["liq"] = (best.get("liquidity") or {}).get("usd")
        info["vol24"] = (best.get("volume") or {}).get("h24")
        created = best.get("pairCreatedAt")
        if created:
            info["age_h"] = max(0.0, (time.time() * 1000 - created) / 3_600_000)
        info["tg"] = info["tg"] or telegram_from_links((best.get("info") or {}).get("socials"))
    return info


def lookup_many(cands):
    """cands: key -> {chain, addr, links, age}. Batched DexScreener lookups (30 per call)."""
    by_chain = {}
    for key, c in list(cands.items())[:MAX_LOOKUPS]:
        by_chain.setdefault(c["chain"], []).append((key, c))
    out = {}
    for chain, items in by_chain.items():
        for i in range(0, len(items), 30):
            chunk = items[i:i + 30]
            try:
                pairs = dex_get(f"/tokens/v1/{chain}/{','.join(c['addr'] for _, c in chunk)}")
            except Exception as exc:
                log.warning("lookup %s failed: %s", chain, exc)
                continue
            grouped = {}
            for p in pairs:
                grouped.setdefault(norm(((p.get("baseToken") or {}).get("address")) or ""), []).append(p)
            for key, c in chunk:
                out[key] = info_from_pairs(chain, c["addr"], grouped.get(norm(c["addr"]), []),
                                           c["links"], c["age"])
    return out


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
        try:
            r = requests.post(url, json=payload, timeout=20)
        except requests.RequestException as exc:
            log.warning("Telegram request failed: %s", exc)
            time.sleep(3)
            continue
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
    chain = CHAIN_NAMES.get(info["chain"].lower(), info["chain"].replace("-", " ").title())
    name = e(info["name"] or info["address"][:8])
    sym = f" · ${e(info['symbol'])}" if info["symbol"] else ""
    meta = f"⛓ {e(chain)}"
    if age_text(info["age_h"]):
        meta += f"   ⏱ {age_text(info['age_h'])} old"
    lines = ["  ".join(f"<b>{t}</b>" for t in tags), "", f"<b>{name}</b>{sym}", meta, ""]
    for icon, label, val in (("💰", "MCap", info["mcap"]), ("💧", "Liquidity", info["liq"]),
                             ("📊", "Volume 24h", info["vol24"])):
        if money(val):
            lines.append(f"{icon} {label}: <b>{money(val)}</b>")
    lines += ["", "📋 <b>Contract</b>", f"<code>{e(info['address'])}</code>", "",
              f'💬 <a href="{e(info["tg"])}">Join Telegram group</a>', ""]
    note = "⚠️ DYOR · Not financial advice"
    if BOOST_TAG in tags:
        note += " · Boosted = paid promotion"
    lines.append(f"<i>{note}</i>")
    return "\n".join(lines)


# ---------- main logic ----------
def run_once(state):
    now = time.time()
    coins = state["coins"]
    first_run = not state.get("initialized")
    seed_only = first_run and not POST_ON_FIRST_RUN

    # --- collect from all sources (each fails independently) ---
    new_src, trend_src, boost_src = {}, {}, {}   # key -> {chain, addr, links, age}

    def add(bucket, chain, addr, links=None, age=None):
        if chain and addr and chain_ok(chain):
            bucket.setdefault(mk(chain, addr), {"chain": chain, "addr": norm(addr),
                                                "links": links, "age": age})

    for page in range(1, GT_NEW_PAGES + 1):
        for chain, addr, age in gt_pools("/networks/new_pools", {"page": page}):
            add(new_src, chain, addr, None, age)
    for chain, addr, age in gt_pools("/networks/trending_pools")[:TRENDING_TOP]:
        add(trend_src, chain, addr, None, age)
    try:
        for p in dex_get("/token-profiles/latest/v1"):
            add(new_src, p.get("chainId"), p.get("tokenAddress"), p.get("links"))
        for b in dex_get("/token-boosts/top/v1")[:TRENDING_TOP]:
            add(boost_src, b.get("chainId"), b.get("tokenAddress"), b.get("links"))
    except Exception as exc:
        log.warning("DexScreener lists failed: %s", exc)
    log.info("new=%d trending=%d boosted=%d known=%d", len(new_src), len(trend_src), len(boost_src), len(coins))
    if not (new_src or trend_src or boost_src):
        log.error("no data from any source")
        return False

    # --- decide which tokens to look up ---
    want = {}
    if seed_only:
        for key in new_src:
            if key not in coins:
                coins[key] = {"tg": None, "first_seen": int(now), "last_checked": int(now),
                              "posted_new": 1, "last_trending": 0}
    else:
        for key, c in new_src.items():
            if key not in coins:
                want[key] = c
    for key, c in coins.items():  # young tokens still missing a Telegram link
        if (not c.get("tg") and not c.get("posted_new") and ":" in key
                and c["first_seen"] > now - RECHECK_NO_TG_HOURS * 3600
                and c["last_checked"] < now - RECHECK_INTERVAL_MIN * 60):
            chain, addr = key.split(":", 1)
            want.setdefault(key, {"chain": chain, "addr": addr, "links": None, "age": None})
    for bucket in (trend_src, boost_src):
        for key, c in bucket.items():
            rec = coins.get(key)
            if not rec or now - rec.get("last_trending", 0) >= COOLDOWN_HOURS * 3600:
                want.setdefault(key, c)
    infos = lookup_many(want)
    log.info("looked up %d tokens, %d have a Telegram link", len(infos), sum(1 for i in infos.values() if i["tg"]))

    # --- build posts ---
    pending = {}

    def queue(key, tag):
        info = infos.get(key)
        if not info or not info["tg"]:
            return
        if MIN_LIQUIDITY_USD and (info["liq"] or 0) < MIN_LIQUIDITY_USD:
            return
        pending.setdefault(key, {"tags": set(), "info": info})["tags"].add(tag)

    for key in new_src:
        if key in coins or key not in infos:
            continue
        info = infos[key]
        if info["age_h"] is None or info["age_h"] > NEW_MAX_AGE_HOURS:
            coins[key] = new_rec(info, now, 1)       # old token: never announce as new
        elif info["tg"]:
            queue(key, NEW_TAG)
        else:
            coins[key] = new_rec(info, now, 0)       # young, no Telegram yet: re-check later
    for key, c in coins.items():
        if key in infos and not c.get("tg") and not c.get("posted_new"):
            c["last_checked"] = int(now)
            queue(key, NEW_TAG)
    for key in trend_src:
        if key in infos:
            queue(key, TREND_TAG)
    for key in boost_src:
        if key in infos:
            queue(key, BOOST_TAG)

    # --- send, then record ---
    sent = 0
    for key, p in pending.items():
        if sent >= MAX_POSTS_PER_RUN:
            log.info("post cap reached; rest retried next run")
            break
        tags = [t for t in (NEW_TAG, TREND_TAG, BOOST_TAG) if t in p["tags"]]
        if not send(build_message(p["info"], tags)):
            continue
        sent += 1
        c = coins.setdefault(key, new_rec(p["info"], now, 0))
        c["tg"], c["posted_new"] = p["info"]["tg"], 1
        if TREND_TAG in tags or BOOST_TAG in tags:
            c["last_trending"] = int(now)
        log.info("posted %s %s", key, tags)

    for key in [k for k, c in coins.items()
                if c["first_seen"] < now - (30 if c.get("tg") else 3) * 86400]:
        del coins[key]
    state["initialized"] = True
    return True


def main():
    state = load_state() or {"coins": {}, "initialized": False}
    end = time.time() + RUN_MINUTES * 60
    while True:
        try:
            run_once(state)
        except Exception:
            log.exception("check failed")  # keep going; what was posted is still saved
        save_state(state)
        if time.time() + INTERVAL_SECONDS >= end:
            break
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
