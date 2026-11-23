#!/usr/bin/env python3
"""Scores past scanner signals: what the price did 15, 30 and 60 minutes later, after fees.
TEST ONLY: reads the scanner logs, writes signal_outcomes.csv. Never changes labels, alerts or orders.

- One signal per coin per hour (repeat rows from back-to-back scans are skipped).
- Prices come from Coinbase public 5-minute candles; if that fails, from the scanner's own logs.
- Only signals at least 65 minutes old are scored, and each is scored once (already-scored rows are kept).
"""
import csv, json, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "signal_outcomes.csv"
LABELS = {"REAL UP", "UP vs TREND", "UP NO VOL", "THIN PUMP", "FADING"}   # up-type labels worth scoring
FEE = 0.018                       # round trip on small orders (same number the buy-watch uses)
HORIZONS = (15, 30, 60)           # minutes
MAX_API = 25                      # Coinbase candle calls per run
API = "https://api.exchange.coinbase.com/products/{}-USD/candles?granularity=300&start={}&end={}"
COLS = ["ts_utc", "asset", "label", "price", "liq", "vol_x", "trend_1h"] + \
       [f"ret_{h}m" for h in HORIZONS] + ["best_60m", "worst_60m", "source"]


def ts(s):
    d = datetime.fromisoformat(s)
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_logs():
    files = sorted((HERE / "logs").glob("scanner_log_*.csv")) + [HERE / "scanner_log_v4.csv"]
    rows = []
    for f in files:
        if f.exists():
            with f.open() as fh:
                rows += list(csv.DictReader(fh))
    return rows


def coinbase_path(asset, t0):
    """[(t, high, low, close)] for the 65 minutes after t0 from Coinbase 5m candles."""
    url = API.format(asset, iso(t0), iso(t0 + 65 * 60))
    req = urllib.request.Request(url, headers={"User-Agent": "cscan"})
    data = json.load(urllib.request.urlopen(req, timeout=15))   # [time, low, high, open, close, vol]
    return sorted((c[0] + 300, c[2], c[1], c[4]) for c in data)  # candle close time


def main():
    now = time.time()
    done = {}
    if OUT.exists():
        with OUT.open() as f:
            for r in csv.DictReader(f):
                done[(r["ts_utc"], r["asset"])] = r

    logs = load_logs()
    logs.sort(key=lambda r: r["ts_utc"])
    px = {}
    for r in logs:
        px.setdefault(r["asset"], []).append((ts(r["ts_utc"]), float(r["price"])))

    last, sigs = {}, []
    for r in logs:
        if r["label"] not in LABELS:
            continue
        t, a = ts(r["ts_utc"]), r["asset"]
        if a in last and t - last[a] < 3600:
            continue
        last[a] = t
        sigs.append(r)

    api_calls, new = 0, 0
    for r in sigs:
        key = (r["ts_utc"], r["asset"])
        t0, p0 = ts(r["ts_utc"]), float(r["price"])
        if now - t0 < 65 * 60:
            continue
        prev = done.get(key)
        if prev and (prev.get("source") == "coinbase" or now - t0 > 24 * 3600):
            continue   # scored for good (log-only scores get retried with real candles for a day)
        out = {k: r.get(k, "") for k in ("ts_utc", "asset", "label", "price", "vol_x", "trend_1h")}
        out["liq"] = r.get("quote_vol_24h", "")
        path, src = None, ""
        if api_calls < MAX_API:
            api_calls += 1
            try:
                path, src = coinbase_path(r["asset"], t0), "coinbase"
                time.sleep(0.2)
            except Exception as e:
                print(f"[warn] candles {r['asset']}: {e}", file=sys.stderr)
        if not path:   # fall back to the scanner's own prices (gaps possible)
            path = [(t, p, p, p) for t, p in px.get(r["asset"], []) if t0 < t <= t0 + 65 * 60]
            src = "log"
        if not path:
            continue   # try again next run
        if prev and src == "log":
            continue   # keep the earlier score
        for h in HORIZONS:
            c = [cl for t, hi, lo, cl in path if abs(t - (t0 + h * 60)) <= 150]
            out[f"ret_{h}m"] = f"{c[0] / p0 - 1 - FEE:.4f}" if c else ""
        win = [x for x in path if x[0] <= t0 + 60 * 60]
        out["best_60m"] = f"{max(x[1] for x in win) / p0 - 1:.4f}" if win else ""
        out["worst_60m"] = f"{min(x[2] for x in win) / p0 - 1:.4f}" if win else ""
        out["source"] = src
        done[key] = out
        new += 1

    rows = sorted(done.values(), key=lambda r: r["ts_utc"])
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print(f"signal_outcomes: {new} newly scored, {len(rows)} total (after {FEE*100:.1f}% fees)")
    for lab in sorted(LABELS):
        v = [float(r["ret_30m"]) for r in rows if r["label"] == lab and r.get("ret_30m")]
        if v:
            print(f"  {lab:12} n={len(v):3}  30m avg {sum(v)/len(v)*100:+.2f}%  "
                  f"profitable {sum(x > 0 for x in v)/len(v)*100:.0f}%")


if __name__ == "__main__":
    main()
