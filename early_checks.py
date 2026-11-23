#!/usr/bin/env python3
"""
early_checks.py  (TEST ONLY - does not change labels, alerts or the live scanner)

Runs after each scan and logs three ideas taken from other open-source scanners:

  1. NEW LISTING  (idea from brianleect/binance-pump-alerts)
     Compares Coinbase's USD/USDC spot markets with the list saved last run
     (known_markets.json). Any new coin is logged to listings_log.csv.
     First run only saves the list, so nothing is flagged then.

  2. EARLY  (idea from Alorse/trading-cli "volume divergence")
     Coins with volume at least 4x normal but a 15m move of 0 to +1%:
     money coming in before the price moves. Read straight from latest.json.

  3. OBV  (idea from darkvolg TrendRider)
     For EARLY and REAL UP coins, On-Balance Volume on 5m candles vs its
     20-candle average. Above = buyers steadily adding. Also logs the last
     closed candle's body ratio (body / high-low range, from trading-cli).

Results go to early_checks_log.csv for the weekly check-in.
"""
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "early_checks_log.csv"
LISTINGS_LOG = HERE / "listings_log.csv"
KNOWN = HERE / "known_markets.json"

EARLY_VOL_X = 4.0        # volume at least 4x normal (2x fired ~6 coins every scan)
EARLY_MAX_MOVE = 0.01    # but 15m move under 1%
MIN_LIQ = 1_000_000      # skip coins under $1M 24h volume
MAX_CANDLE_CALLS = 12    # keep each run fast
STABLES = {"USDT", "USDC", "EURC", "DAI", "PYUSD", "USDS", "USDE", "FDUSD", "TUSD", "USD1"}


def ema(values, n):
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def obv_check(candles, n=20):
    """True if OBV is above its n-period EMA on closed candles. None if too little data."""
    if len(candles) < n + 2:
        return None, None
    obv = [0.0]
    for prev, cur in zip(candles, candles[1:]):
        c0, c1, v = prev[4], cur[4], cur[5]
        obv.append(obv[-1] + (v if c1 > c0 else -v if c1 < c0 else 0))
    line = ema(obv, n)
    return obv[-1] > line[-1], round(obv[-1] - line[-1], 2)


def body_ratio(candle):
    _, o, h, l, c, _ = candle
    rng = h - l
    return round(abs(c - o) / rng, 2) if rng > 0 else None


def check_listings(ex, scan):
    """Log Coinbase coins that weren't there last run."""
    try:
        ex.load_markets()
    except Exception as e:
        print(f"[early_checks] markets: {e}", file=sys.stderr)
        return
    now = sorted({m["base"] for m in ex.markets.values()
                  if m.get("spot") and m.get("quote") in ("USD", "USDC")
                  and m["base"] not in STABLES})
    if len(now) < 50:            # bad or partial answer: don't overwrite the list
        print(f"[early_checks] only {len(now)} markets returned, listing check skipped")
        return
    try:
        old = set(json.loads(KNOWN.read_text()))
    except Exception:
        KNOWN.write_text(json.dumps(now))
        print(f"[early_checks] saved {len(now)} Coinbase coins (first run, nothing flagged)")
        return
    new = [b for b in now if b not in old]
    gone = len(old - set(now))
    if new:
        first = not LISTINGS_LOG.exists()
        with LISTINGS_LOG.open("a", newline="") as f:
            w = csv.writer(f)
            if first:
                w.writerow(["scan_utc", "asset"])
            w.writerows([[scan, b] for b in new])
        print(f"[early_checks] NEW LISTING: {', '.join(new)}")
    # keep coins that briefly vanish (maintenance) so they aren't re-flagged later
    KNOWN.write_text(json.dumps(sorted(old | set(now))))
    print(f"[early_checks] listings: {len(new)} new, {gone} missing this run")


def main():
    try:
        data = json.loads((HERE / "latest.json").read_text())
    except Exception as e:
        print(f"[early_checks] no latest.json: {e}")
        return
    scan = data.get("scan_utc")
    if LOG.exists() and scan and scan in LOG.read_text():
        print("[early_checks] this scan was already checked (scan step may have failed)")
        return

    import ccxt
    ex = ccxt.coinbase({"enableRateLimit": True})
    check_listings(ex, scan)

    rows = [r for r in data.get("rows", []) if r.get("cb") and (r.get("liq") or 0) >= MIN_LIQ]
    picks = []
    for r in rows:
        early = (r.get("vol_x") or 0) >= EARLY_VOL_X and 0 <= (r.get("ret_15m") or 0) < EARLY_MAX_MOVE
        if early:
            picks.append(("EARLY", r))
        elif r.get("label") == "REAL UP":
            picks.append(("REAL UP", r))
    if not picks:
        print("[early_checks] no EARLY or REAL UP coins this scan")
        return
    picks.sort(key=lambda p: -(p[1].get("vol_x") or 0))

    out = []
    for kind, r in picks[:MAX_CANDLE_CALLS]:
        try:
            c5 = ex.fetch_ohlcv(r["cb"], "5m", limit=60)[:-1]     # drop forming candle
        except Exception as e:
            print(f"[early_checks] {r['cb']}: {e}", file=sys.stderr)
            continue
        ok, gap = obv_check(c5)
        br = body_ratio(c5[-1]) if c5 else None
        out.append([scan, r["asset"], kind, r.get("label"), r.get("price"), r.get("ret_15m"),
                    r.get("vol_x"), r.get("liq"), ok, gap, br])
        print(f"[early_checks] {kind} {r['asset']}: vol x{(r.get('vol_x') or 0):.1f}, "
              f"15m {(r.get('ret_15m') or 0)*100:+.2f}%, OBV {'rising' if ok else 'falling' if ok is False else '?'}, "
              f"body {br}")

    if out:
        first = not LOG.exists()
        with LOG.open("a", newline="") as f:
            w = csv.writer(f)
            if first:
                w.writerow(["scan_utc", "asset", "kind", "label", "price", "ret_15m", "vol_x",
                            "liq", "obv_ok", "obv_gap", "body_ratio"])
            w.writerows(out)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:                        # never break the scan workflow
        print(f"[early_checks] error: {e}", file=sys.stderr)
