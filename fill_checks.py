#!/usr/bin/env python3
"""
fill_checks.py  (TEST ONLY - never changes labels, alerts or orders)

Answers: could a $5 trade really be done at limit-order prices, and how much does the spread cost?
 1. LOG: after each scan, for flagged Coinbase coins (REAL UP, REAL DOWN, THIN PUMP, UP NO VOL, UP vs TREND)
    record the live best bid/ask, spread % and $ size at the top of the book  -> fill_log.csv
    (one row per coin per hour, max 10 coins per run)
 2. SCORE (rows 65+ minutes old, each once): from Coinbase 5-minute candles
    - would a limit BUY resting at the bid have filled within 15 min?  (candle low <= bid)
    - return 30 and 60 min later if bought at the bid vs at the ask (gross, before fees)
    -> fill_outcomes.csv
Compare these with signal_outcomes.csv to see whether limit orders (about 1.0% round-trip fees) can beat market orders (1.8%).
"""
import csv, json, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "fill_log.csv"
OUT = HERE / "fill_outcomes.csv"
LABELS = {"REAL UP", "REAL DOWN", "THIN PUMP", "UP NO VOL", "UP vs TREND"}
MAX_COINS, MAX_SCORE = 10, 15
LOG_COLS = ["ts_utc", "asset", "label", "bid", "ask", "spread_pct", "bid_usd", "ask_usd"]
OUT_COLS = ["ts_utc", "asset", "label", "spread_pct", "filled_15m", "fill_min",
            "ret30_from_bid", "ret30_from_ask", "ret60_from_bid", "ret60_from_ask"]
API = "https://api.exchange.coinbase.com/products/{}-USD/"


def get(url):
    r = urllib.request.Request(url, headers={"User-Agent": "cscan-fill-checks"})
    return json.load(urllib.request.urlopen(r, timeout=15))


def ts(s):
    d = datetime.fromisoformat(s)
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read(path):
    return list(csv.DictReader(path.open())) if path.exists() else []


def append(path, cols, rows):
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(cols)
        w.writerows(rows)


def log_step():
    data = json.loads((HERE / "latest.json").read_text())
    now = data.get("scan_utc") or datetime.now(timezone.utc).isoformat()
    t_now = ts(now)
    recent = {r["asset"] for r in read(LOG) if t_now - ts(r["ts_utc"]) < 3600}   # one per coin per hour
    rows = [r for r in data.get("rows", []) if r.get("label") in LABELS and r.get("cb") and r["asset"] not in recent]
    rows.sort(key=lambda r: abs(r.get("ret_15m") or 0), reverse=True)
    out = []
    for r in rows[:MAX_COINS]:
        try:
            b = get(API.format(r["asset"]) + "book?level=1")
            bid, bs = float(b["bids"][0][0]), float(b["bids"][0][1])
            ask, asz = float(b["asks"][0][0]), float(b["asks"][0][1])
        except Exception as e:
            print(f"[fill_checks] {r['asset']}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        spr = (ask - bid) / ((ask + bid) / 2) * 100
        out.append([now, r["asset"], r["label"], bid, ask, round(spr, 3), round(bid * bs, 2), round(ask * asz, 2)])
        print(f"[fill_checks] {r['asset']}: spread {spr:.2f}%  top of book ${bid*bs:,.0f} / ${ask*asz:,.0f}")
        time.sleep(0.2)
    if out:
        append(LOG, LOG_COLS, out)


def score_step():
    done = {(r["ts_utc"], r["asset"]) for r in read(OUT)}
    now = time.time()
    todo = [r for r in read(LOG) if (r["ts_utc"], r["asset"]) not in done and now - ts(r["ts_utc"]) >= 65 * 60]
    out = []
    for r in todo[:MAX_SCORE]:
        t0 = ts(r["ts_utc"]); bid, ask = float(r["bid"]), float(r["ask"])
        try:
            c = get(API.format(r["asset"]) + f"candles?granularity=300&start={iso(t0 - 300)}&end={iso(t0 + 3900)}")
        except Exception as e:
            print(f"[fill_checks] score {r['asset']}: {e}", file=sys.stderr)
            continue
        c = sorted(c)                                         # [time, low, high, open, close, volume]
        after = [x for x in c if x[0] >= t0 // 300 * 300]
        if len(after) < 8:
            continue
        fill_min = next((int((x[0] - t0) // 60) if x[0] > t0 else 0 for x in after if x[0] < t0 + 900 and x[1] <= bid), "")
        def close_at(m):
            k = [x for x in after if x[0] + 300 <= t0 + m * 60]
            return k[-1][4] if k else None
        c30, c60 = close_at(30), close_at(60)
        f = lambda c_, p: "" if c_ is None else round(c_ / p - 1, 4)
        out.append([r["ts_utc"], r["asset"], r["label"], r["spread_pct"], int(fill_min != ""), fill_min,
                    f(c30, bid), f(c30, ask), f(c60, bid), f(c60, ask)])
        time.sleep(0.2)
    if out:
        append(OUT, OUT_COLS, out)


if __name__ == "__main__":
    for step in (log_step, score_step):
        try:
            step()
        except Exception as e:                                # never break the scan workflow
            print(f"[fill_checks] {step.__name__} error: {type(e).__name__}: {e}", file=sys.stderr)
