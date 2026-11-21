#!/usr/bin/env python3
"""
crypto_movement_scanner.py
=============================
Same as the v3 scanner, plus MULTI-TIMEFRAME CONFIRMATION.

A REAL UP / REAL DOWN now also has to agree with the bigger trend:
  * 1h trend: latest 1h close vs its 20-hour average
  * 1d trend: latest daily close vs its 10-day average
If a 15-minute move goes AGAINST either trend it is relabeled
"UP vs TREND" / "DOWN vs TREND" and NO phone alert is sent.
If trend data can't be fetched, the original label is kept (shown with ?).

The 'trend' column shows 1h then 1d: + = up, - = down, ? = unknown.

Uses its own state/log files (scanner_state.json, scanner_log_v4.csv) so your
v3 files are not touched.

Usage:
  python3 -u crypto_movement_scanner_v4.py --loop 120
  python3 -u crypto_movement_scanner_v4.py --once --no-mtf     # turn the trend check off
"""
import argparse
import asyncio
import csv
import json
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt

EXCHANGES = ["coinbase", "kraken", "binanceus", "bitstamp"]
QUOTES = {"USD", "USDT", "USDC"}
SUBREDDITS = ["CryptoCurrency", "CryptoMarkets", "altcoin", "CryptoMoonShots", "SatoshiStreetBets"]
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "scanner_state_v4.json"
LOG_FILE = HERE / "scanner_log_v4.csv"
MIN_HISTORY = 3
DISCORD_TIMEOUT = 60

STABLES = {"USDT", "USDC", "EURC", "DAI", "PYUSD", "USDS", "USDE", "FDUSD", "TUSD", "USD1"}
AMBIGUOUS = {"ONE", "NEAR", "LINK", "SUI", "OP", "ARB", "GAS", "SAND", "MINA", "FLOW", "ICP",
             "AMP", "ENS", "FET", "GAL", "RUNE", "SEI", "TIA", "JUP", "PYTH", "WLD", "ME"}
STOPWORDS = {"USD", "USDT", "USDC", "THE", "AND", "FOR", "ALL", "NEW", "CAN", "NOT", "BUY",
             "SELL", "ATH", "DCA", "FUD", "FOMO", "CEO", "ETF", "SEC", "IMO", "API", "NFT"}
MAJORS = {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "DOT", "LTC", "BCH", "XLM",
          "SHIB", "PEPE", "TRX", "HBAR", "XMR", "ATOM", "UNI", "AAVE", "BONK"}


# ----------------------------------------------------------------- exchanges
def split_symbol(symbol):
    if "/" not in symbol or ":" in symbol:
        return None
    base, quote = symbol.split("/")
    return (base, quote) if quote in QUOTES else None


def candle_metrics(candles):
    """candles: ccxt OHLCV rows [ts, o, h, l, c, v]. Drops the still-forming last candle."""
    rows = candles[:-1]
    if len(rows) < 25:
        return None
    closes = [r[4] for r in rows]
    vols = [r[5] for r in rows]
    prev = vols[-21:-1]
    mean, sd = statistics.mean(prev), statistics.pstdev(prev)
    vol_z = (vols[-1] - mean) / sd if sd > 0 else 0.0
    return {
        "ret_15m": closes[-1] / closes[-4] - 1,
        "ret_1h": closes[-1] / closes[-13] - 1,
        "vol_z": vol_z,
        "price": closes[-1],
    }


def scan_exchange(ex_id, top_n):
    out = {}
    try:
        ex = getattr(ccxt, ex_id)({"enableRateLimit": True})
        tickers = ex.fetch_tickers()
    except Exception as e:
        print(f"[warn] {ex_id}: {e}", file=sys.stderr)
        return out
    ranked = []
    for sym, t in tickers.items():
        parts = split_symbol(sym)
        if not parts:
            continue
        qv = t.get("quoteVolume") or ((t.get("baseVolume") or 0) * (t.get("last") or 0))
        if qv and parts[0] not in STABLES:
            ranked.append((qv, sym, parts[0]))
    for qv, sym, base in sorted(ranked, reverse=True)[:top_n]:
        try:
            m = candle_metrics(ex.fetch_ohlcv(sym, "5m", limit=40))
        except Exception:
            continue
        if m and base not in out:
            m["quote_vol_24h"] = qv
            out[base] = m
    return out


def merge_exchanges(per_ex):
    merged = defaultdict(dict)
    for ex_id, data in per_ex.items():
        for base, m in data.items():
            merged[base][ex_id] = m
    return merged


# ------------------------------------------------- multi-timeframe confirmation
_EX_CACHE = {}


def _sma_trend(ex, sym, timeframe, limit, n):
    """+1 if the last closed candle is above its n-candle average, -1 if below."""
    rows = ex.fetch_ohlcv(sym, timeframe, limit=limit)[:-1]
    closes = [r[4] for r in rows]
    if len(closes) < n:
        return None
    return 1 if closes[-1] > statistics.mean(closes[-n:]) else -1


def trend_check(base):
    """Returns (trend_1h, trend_1d), each +1 / -1 / None. Tries Coinbase first, then the others."""
    for ex_id in EXCHANGES:
        try:
            ex = _EX_CACHE.get(ex_id)
            if ex is None:
                ex = _EX_CACHE[ex_id] = getattr(ccxt, ex_id)({"enableRateLimit": True})
            ex.load_markets()
            sym = next((f"{base}/{q}" for q in ("USD", "USDC", "USDT") if f"{base}/{q}" in ex.markets), None)
            if not sym:
                continue
            return (_sma_trend(ex, sym, "1h", 40, 20), _sma_trend(ex, sym, "1d", 30, 10))
        except Exception as e:
            print(f"[warn] trend {base} on {ex_id}: {e}", file=sys.stderr)
    return (None, None)


def apply_trend(rows):
    """Relabel REAL UP/DOWN moves that fight the 1h or 1d trend."""
    for r in rows:
        if r["label"] not in ("REAL UP", "REAL DOWN"):
            continue
        t1h, t1d = trend_check(r["asset"])
        r["t1h"], r["t1d"] = t1h, t1d
        want = 1 if r["label"] == "REAL UP" else -1
        if (t1h is not None and t1h != want) or (t1d is not None and t1d != want):
            r["label"] = "UP vs TREND" if want == 1 else "DOWN vs TREND"


def trend_str(r):
    sym = {1: "+", -1: "-", None: "?"}
    return sym[r.get("t1h")] + sym[r.get("t1d")]


# -------------------------------------------------------------------- social
def extract_symbols(text, symbols):
    found = set()
    for m in re.finditer(r"(\$)?\b([A-Za-z0-9]{1,10})\b", text):
        dollar, raw = m.group(1), m.group(2)
        word = raw.upper()
        if not re.search(r"[A-Z]", word):
            continue
        if word in STOPWORDS or word not in symbols:
            continue
        if dollar:
            found.add(word)
        elif len(word) < 2 or word in AMBIGUOUS:
            continue
        elif raw == word or word in MAJORS:
            found.add(word)
    return found


def reddit_mentions(symbols, minutes):
    cid, secret = os.getenv("REDDIT_CLIENT_ID"), os.getenv("REDDIT_CLIENT_SECRET")
    if not (cid and secret):
        return Counter()
    import praw
    reddit = praw.Reddit(client_id=cid, client_secret=secret,
                         user_agent=os.getenv("REDDIT_USER_AGENT", "movement-scanner/1.0"))
    cutoff = time.time() - minutes * 60
    counts = Counter()
    for sub in SUBREDDITS:
        try:
            for post in reddit.subreddit(sub).new(limit=200):
                if post.created_utc < cutoff:
                    break
                counts.update(extract_symbols(f"{post.title} {post.selftext or ''}", symbols))
        except Exception as e:
            print(f"[warn] reddit r/{sub}: {e}", file=sys.stderr)
    return counts


def discord_mentions(symbols, minutes):
    token = os.getenv("DISCORD_BOT_TOKEN")
    ids = {int(x) for x in os.getenv("DISCORD_CHANNEL_IDS", "").split(",") if x.strip()}
    if not (token and ids):
        return Counter()
    try:
        import discord
    except ImportError:
        print("[warn] discord: discord.py not installed (pip install discord.py)", file=sys.stderr)
        return Counter()
    intents = discord.Intents.default()
    intents.message_content = True
    counts = Counter()
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    async def read_channels():
        async with discord.Client(intents=intents) as client:
            await client.login(token)
            for cid in ids:
                try:
                    ch = await client.fetch_channel(cid)
                    async for m in ch.history(after=since, limit=2000):
                        if not m.author.bot:
                            counts.update(extract_symbols(m.content, symbols))
                except Exception as e:
                    print(f"[warn] discord channel {cid}: {e}", file=sys.stderr)

    try:
        asyncio.run(asyncio.wait_for(read_channels(), timeout=DISCORD_TIMEOUT))
    except Exception as e:
        print(f"[warn] discord: {e or 'timed out'}", file=sys.stderr)
    return counts


def x_spike(symbol):
    token = os.getenv("X_BEARER_TOKEN")
    if not token:
        return None
    import requests
    try:
        r = requests.get("https://api.twitter.com/2/tweets/counts/recent",
                         headers={"Authorization": f"Bearer {token}"},
                         params={"query": f"${symbol} -is:retweet lang:en", "granularity": "hour"},
                         timeout=15)
        r.raise_for_status()
        counts = [b["tweet_count"] for b in r.json()["data"]]
        if len(counts) < 6:
            return None
        return counts[-2] / (statistics.mean(counts[:-2]) + 1)
    except Exception:
        return None


def coingecko_trending():
    import requests
    try:
        r = requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=15)
        r.raise_for_status()
        return {c["item"]["symbol"].upper(): i + 1 for i, c in enumerate(r.json().get("coins", []))}
    except Exception as e:
        print(f"[warn] coingecko trending: {e}", file=sys.stderr)
        return {}


def mention_spikes(current, state, key, keep=24):
    runs = state.get(key + "_runs", 0)
    hist = state.setdefault(key, {})
    spikes = {}
    for sym in set(current) | set(hist):
        past = hist.get(sym, [0] * min(runs, keep))
        if len(past) >= MIN_HISTORY:
            spikes[sym] = (current.get(sym, 0) + 1) / (statistics.mean(past) + 1)
        new_hist = (past + [current.get(sym, 0)])[-keep:]
        if any(new_hist):
            hist[sym] = new_hist
        else:
            hist.pop(sym, None)
    state[key + "_runs"] = runs + 1
    return spikes


# ---------------------------------------------------------------- classifying
def classify(per_ex, a):
    flagged = {e: m for e, m in per_ex.items()
               if abs(m["ret_15m"]) >= a.move_pct / 100 or m["vol_z"] >= a.vol_z}
    if not flagged:
        return None
    liq = max(m["quote_vol_24h"] for m in per_ex.values())
    movers = [m for m in flagged.values() if abs(m["ret_15m"]) >= a.move_pct / 100]
    ups = sum(1 for m in movers if m["ret_15m"] > 0)
    downs = len(movers) - ups
    best = max(movers or flagged.values(), key=lambda m: abs(m["ret_15m"]))
    down = downs > ups if ups != downs else best["ret_15m"] < 0
    if movers:
        best = max((m for m in movers if (m["ret_15m"] < 0) == down), key=lambda m: abs(m["ret_15m"]))
    confirmed = max(ups, downs) >= 2 and any(m["vol_z"] >= a.vol_z for m in flagged.values())
    if confirmed and liq >= a.min_liq:
        label = "REAL DOWN" if down else "REAL UP"
    elif movers and (len(flagged) == 1 or liq < a.min_liq):
        label = "THIN DROP" if down else "THIN PUMP"
    else:
        label = "WATCH"
    return label, {"ret_15m": best["ret_15m"], "ret_1h": best["ret_1h"],
                   "vol_z": max(m["vol_z"] for m in flagged.values()),
                   "n_flagged": len(flagged), "n_listed": len(per_ex), "liq": liq,
                   "price": best["price"]}


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")


def push_alerts(rows, state):
    """Phone push (via ntfy.sh) for each REAL UP that also passed the trend check, once per coin per hour."""
    import requests
    if not NTFY_TOPIC:
        print("  >> no NTFY_TOPIC set, push skipped")
        return
    sent = state.setdefault("pushed", {})
    now = time.time()
    for r in rows:
        if r["label"] != "REAL UP":
            continue
        if now - sent.get(r["asset"], 0) < 3600:
            continue
        msg = (f"{r['asset']} +{r['ret_15m']*100:.2f}% in 15m, volume x{r['vol_z']:.1f}, "
               f"confirmed on {r['n_flagged']}/{r['n_listed']} exchanges, trend 1h/1d {trend_str(r)}")
        try:
            requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode(),
                          headers={"Title": f"BUY CANDIDATE: {r['asset']}", "Priority": "high",
                                   "Tags": "chart_with_upwards_trend"}, timeout=10)
            sent[r["asset"]] = now
            print(f"  >> push sent for {r['asset']}")
        except Exception as e:
            print(f"  >> push failed: {e}")


def run_once(a):
    started = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=len(EXCHANGES)) as pool:
        per_ex = dict(zip(EXCHANGES, pool.map(lambda e: scan_exchange(e, a.top), EXCHANGES)))
    merged = merge_exchanges(per_ex)
    symbols = set(merged)
    state = load_state()

    reddit, discord, trending = {}, {}, {}
    if not a.no_social:
        trending = coingecko_trending()
        reddit = mention_spikes(reddit_mentions(symbols, a.window), state, "reddit")
        discord = mention_spikes(discord_mentions(symbols, a.window), state, "discord")
    rows = []
    for base, data in merged.items():
        res = classify(data, a)
        social_hot = (reddit.get(base, 0) >= a.social_spike) or (discord.get(base, 0) >= a.social_spike) \
            or (base in trending)
        if not res:
            if social_hot:
                best = max(data.values(), key=lambda m: m["quote_vol_24h"])
                res = ("CHATTER", {"ret_15m": best["ret_15m"], "ret_1h": best["ret_1h"],
                                   "vol_z": best["vol_z"], "n_flagged": 0, "n_listed": len(data),
                                   "liq": best["quote_vol_24h"], "price": best["price"]})
            else:
                continue
        label, s = res
        if label == "WATCH" and social_hot:
            label = "CHATTER"
        rows.append({"asset": base, "label": label, **s,
                     "reddit": reddit.get(base, 0), "discord": discord.get(base, 0), "x": None,
                     "cg": trending.get(base), "t1h": None, "t1d": None})

    if not a.no_mtf:
        apply_trend(rows)

    order = {"REAL UP": 0, "REAL DOWN": 1, "UP vs TREND": 2, "DOWN vs TREND": 3,
             "CHATTER": 4, "THIN PUMP": 5, "THIN DROP": 6, "WATCH": 7}
    rows.sort(key=lambda r: (order[r["label"]], -abs(r["ret_15m"])))

    if not a.no_push:
        push_alerts(rows, state)
    STATE_FILE.write_text(json.dumps(state))

    if not a.no_social and os.getenv("X_BEARER_TOKEN"):
        for r in rows[:a.x_max]:
            r["x"] = x_spike(r["asset"])
    print(f"\n=== {started:%Y-%m-%d %H:%M:%S} UTC | {len(rows)} flagged | exchanges ok: "
          f"{', '.join(e for e, d in per_ex.items() if d) or 'none'}")
    print(f"{'ASSET':8} {'LABEL':14} {'15m':>7} {'1h':>7} {'volZ':>5} {'exch':>5} {'trnd':>4} "
          f"{'24h vol $':>12} {'rdt':>5} {'dsc':>5} {'x':>5} {'cg':>3}")
    for r in rows[:a.show]:
        xs = f"{r['x']:.1f}" if r["x"] is not None else "-"
        print(f"{r['asset']:8} {r['label']:14} {r['ret_15m']*100:6.2f}% {r['ret_1h']*100:6.2f}% "
              f"{r['vol_z']:5.1f} {str(r['n_flagged']) + '/' + str(r['n_listed']):>5} {trend_str(r):>4} "
              f"{r['liq']:12,.0f} {r['reddit']:5.1f} {r['discord']:5.1f} {xs:>5} {r['cg'] or '-':>3}")

    new = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts_utc", "asset", "label", "price", "ret_15m", "ret_1h", "vol_z",
                        "n_flagged", "n_listed", "quote_vol_24h", "reddit", "discord", "x", "cg_rank",
                        "trend_1h", "trend_1d"])
        for r in rows:
            w.writerow([started.isoformat(), r["asset"], r["label"], r["price"], r["ret_15m"],
                        r["ret_1h"], r["vol_z"], r["n_flagged"], r["n_listed"], r["liq"],
                        r["reddit"], r["discord"], r["x"], r["cg"], r["t1h"], r["t1d"]])


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--once", action="store_true", help="run one scan and exit")
    p.add_argument("--loop", type=int, metavar="SECONDS", help="repeat every N seconds")
    p.add_argument("--top", type=int, default=50, help="top N pairs per exchange by 24h volume")
    p.add_argument("--move-pct", type=float, default=1.5, help="15-minute move that counts as unusual")
    p.add_argument("--vol-z", type=float, default=3.0, help="5-minute volume z-score that counts as a spike")
    p.add_argument("--min-liq", type=float, default=5_000_000, help="24h USD volume below this is 'thin'")
    p.add_argument("--social-spike", type=float, default=3.0, help="mention ratio vs. baseline that counts as hot")
    p.add_argument("--window", type=int, default=60, help="minutes of social history to read")
    p.add_argument("--show", type=int, default=25, help="max rows to print")
    p.add_argument("--no-push", action="store_true", help="do not send phone alerts")
    p.add_argument("--no-social", action="store_true", help="skip Reddit/Discord/X")
    p.add_argument("--no-mtf", action="store_true", help="skip the 1h/1d trend confirmation")
    p.add_argument("--x-max", type=int, default=10, help="max flagged coins to check on X per run")
    a = p.parse_args()
    if not (a.once or a.loop):
        a.once = True
    while True:
        started = time.time()
        try:
            run_once(a)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            if a.once:
                raise
            print(f"[error] scan failed, will retry next round: {e}", file=sys.stderr)
        if a.once:
            break
        took = time.time() - started
        if took > a.loop:
            print(f"[warn] scan took {took:.0f}s, longer than --loop {a.loop}s", file=sys.stderr)
        time.sleep(max(0, a.loop - took))


if __name__ == "__main__":
    main()