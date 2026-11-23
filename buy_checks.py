#!/usr/bin/env python3
"""
buy_checks.py  (TEST ONLY - does not change labels, alerts or the live scanner)

After each scan, looks at every REAL UP coin in latest.json and runs two extra checks:
  1. RSI (14) on 15-minute candles  -> passes if RSI <= 75 (not overbought)
  2. Up/Down volume over the last 15 one-minute candles
     -> share of volume on green candles; passes if >= 55% (buyers, not sellers)

Each result is appended to buy_checks_log.csv so we can later compare
coins that passed vs failed against what the price did next.
"""
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "buy_checks_log.csv"
ERR_LOG = HERE / "buy_checks_errors.csv"   # one row per coin that could not be checked
MAX_COINS = 10                              # keep busy scans (market-wide jumps) inside rate limits
RSI_MAX = 75
UP_SHARE_MIN = 0.55


def rsi(closes, n=14):
    """Wilder's RSI on a list of closes (oldest first). None if not enough data."""
    if len(closes) < n + 1:
        return None
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + l) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def up_share(candles):
    """Share of volume on green candles (close > open); flat candles count half."""
    up = tot = 0.0
    for _, o, _, _, c, v in candles:
        tot += v
        up += v if c > o else v / 2 if c == o else 0
    return up / tot if tot > 0 else None


def main():
    try:
        data = json.loads((HERE / "latest.json").read_text())
    except Exception as e:
        print(f"[buy_checks] no latest.json: {e}")
        return
    scan = data.get("scan_utc")
    if LOG.exists() and scan and scan in LOG.read_text():
        print("[buy_checks] this scan was already checked (scan step may have failed)")
        return
    rows = [r for r in data.get("rows", []) if r.get("label") == "REAL UP" and r.get("cb")]
    if not rows:
        print("[buy_checks] no REAL UP coins this scan")
        return

    rows.sort(key=lambda r: r.get("ret_15m") or 0, reverse=True)
    rows = rows[:MAX_COINS]
    import time
    import ccxt
    ex = ccxt.coinbase({"enableRateLimit": True, "timeout": 15000})
    out, errs = [], []
    for r in rows:
        sym = r["cb"]
        c15 = c1 = None
        for attempt in (1, 2):                    # one retry after a short pause
            try:
                c15 = ex.fetch_ohlcv(sym, "15m", limit=60)[:-1]      # drop forming candle
                c1 = ex.fetch_ohlcv(sym, "1m", limit=16)[:-1][-15:]
                break
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"[:200]
                time.sleep(2)
        if c15 is None or c1 is None:
            print(f"[buy_checks] {sym}: {last_err}", file=sys.stderr)
            errs.append([data.get("scan_utc"), r["asset"], last_err])
            continue
        rv = rsi([c[4] for c in c15])
        us = up_share(c1)
        rsi_ok = rv is not None and rv <= RSI_MAX
        vol_ok = us is not None and us >= UP_SHARE_MIN
        out.append([data.get("scan_utc"), r["asset"], r.get("price"), r.get("ret_15m"), r.get("vol_x"),
                    None if rv is None else round(rv, 1), None if us is None else round(us, 3),
                    rsi_ok, vol_ok, rsi_ok and vol_ok])
        print(f"[buy_checks] {r['asset']}: RSI {rv if rv is None else round(rv)} "
              f"({'ok' if rsi_ok else 'HIGH'}), up-volume {us if us is None else f'{us:.0%}'} "
              f"({'ok' if vol_ok else 'SELLING'}) -> {'PASS' if rsi_ok and vol_ok else 'FAIL'}")

    if errs:
        new = not ERR_LOG.exists()
        with ERR_LOG.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["scan_utc", "asset", "error"])
            w.writerows(errs)

    if out:
        new = not LOG.exists()
        with LOG.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["scan_utc", "asset", "price", "ret_15m", "vol_x", "rsi15",
                            "up_share", "rsi_ok", "vol_ok", "both_ok"])
            w.writerows(out)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:                       # never break the scan workflow
        print(f"[buy_checks] error: {e}", file=sys.stderr)
        try:
            with ERR_LOG.open("a", newline="") as f:
                csv.writer(f).writerow([datetime.now(timezone.utc).isoformat(), "ALL", f"{type(e).__name__}: {e}"[:200]])
        except Exception:
            pass