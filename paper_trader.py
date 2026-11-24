#!/usr/bin/env python3
"""Paper trader for the cscan watch list. FAKE money only: $10 per buy, no orders are ever placed.
ONE rule for every coin: 15m move >= +3%, volume >= 5x normal, last closed 5m candle still rising,
  1h trend up, and coin not already up 15%+ on the day.
Exit: +5% target (limit order, maker fee), -2% stop (market, taker fee), or after 1 hour (market).
Stop is checked first if both hit in one candle.
  python paper_trader.py backtest  -> tests the rule (plus 3 target sizes for comparison) on recent 5m candles
                                      across all watch-list coins together; writes backtest_results.csv
  python paper_trader.py live      -> run every 5 min: opens/closes paper buys, logs to paper_log.csv
Reads latest.txt (scanner output). Coinbase public market data only."""
import csv, json, os, sys, time, urllib.request
from datetime import datetime, timezone
NOTIONAL = 10.0
FEE = 0.009            # taker (market order) fee per side
MAKER = 0.005          # maker (limit order) fee, used for target exits
SPREAD = 0.0015        # assumed slippage per side when backtesting
RET15, VOLX = 0.03, 5.0
MAX_24H = 15.0
TP, SL = 0.05, 0.02
MAX_HOLD = 12          # 5m candles = 1 hour
COOLDOWN = 3600        # seconds before the same coin can be bought again
G = 300

def get(url):
    r = urllib.request.Request(url, headers={"User-Agent": "paper-trader"})
    return json.load(urllib.request.urlopen(r, timeout=20))

def candles(coin):
    """Closed 5m candles only, oldest first, with gaps (no-trade intervals) filled flat so indexes equal time."""
    d = get(f"https://api.exchange.coinbase.com/products/{coin}-USD/candles?granularity={G}")
    d.sort(key=lambda c: c[0])
    cut = int(time.time()) // G * G            # start of the still-forming candle
    d = [c for c in d if c[0] < cut]
    out = []
    for c in d:
        while out and c[0] - out[-1]["t"] > G:
            p = out[-1]["c"]; out.append(dict(t=out[-1]["t"]+G, lo=p, hi=p, o=p, c=p, v=0.0))
        out.append(dict(t=c[0], lo=c[1], hi=c[2], o=c[3], c=c[4], v=c[5]))
    return out

def watchlist(path="latest.txt", min_vol=1_000_000):
    out = []   # coins on Coinbase, enough volume, and not a runner (24h change under MAX_24H)
    for ln in open(path):
        p = ln.split()
        if len(p) >= 11 and p[0] != "ASSET" and p[10] == "Y":
            try:
                if float(p[8].replace(",", "")) >= min_vol and float(p[9].rstrip("%")) < MAX_24H: out.append(p[0])
            except ValueError: pass
    return out

def signal(cs, i, ret_min=RET15, vol_x=VOLX):
    if i < 21: return False
    k = cs[i]
    ret = k["c"] / cs[i-3]["c"] - 1
    avg = sum(c["v"] for c in cs[i-20:i]) / 20
    rng = k["hi"] - k["lo"]
    rising = k["c"] > k["o"] and k["c"] > cs[i-1]["c"] and rng > 0 and (k["c"] - k["lo"]) >= 0.5*rng
    trend = i >= 48 and k["c"] / cs[i-12]["c"] > 1 and k["c"] > sum(c["c"] for c in cs[i-48:i]) / 48
    return ret >= ret_min and avg > 0 and k["v"] >= vol_x*avg and rising and trend

def net(entry, exitp, maker_exit=False):
    return NOTIONAL*(exitp/entry - 1) - NOTIONAL*FEE - NOTIONAL*(exitp/entry)*(MAKER if maker_exit else FEE)

def simulate(cs, i, tp=TP, sl=SL):
    if i+1 >= len(cs): return None
    entry = cs[i+1]["o"] * (1+SPREAD)
    tpp, slp = entry*(1+tp), entry*(1-sl)
    last = min(i+MAX_HOLD, len(cs)-1)
    for j in range(i+1, last+1):
        if cs[j]["lo"] <= slp: return net(entry, slp*(1-SPREAD)), "stop"
        if cs[j]["hi"] >= tpp: return net(entry, tpp, True), "target"
    if last < i+MAX_HOLD: return None            # trade not finished yet; skip
    return net(entry, cs[last]["c"]*(1-SPREAD)), "time"

def backtest():
    data = {}
    for coin in watchlist():
        try: data[coin] = candles(coin); time.sleep(0.4)
        except Exception as e: print(coin, "skip", e)
    rows = []
    for tp in (0.03, 0.05, 0.07):
        n = w = 0; tot = 0.0; why = {"target": 0, "stop": 0, "time": 0}
        for coin, cs in data.items():
            last = -99
            for i in range(48, len(cs)-1):
                if i - last < MAX_HOLD or not signal(cs, i): continue
                r = simulate(cs, i, tp, SL)
                if r: n += 1; w += r[0] > 0; tot += r[0]; why[r[1]] += 1; last = i
        rows.append([f"{tp:.0%}", f"{SL:.0%}", n, w, round(tot, 3), round(tot/n, 3) if n else 0, why["target"], why["stop"], why["time"]])
    with open("backtest_results.csv", "w", newline="") as f:
        wr = csv.writer(f); wr.writerow(["target", "stop", "trades", "wins", "net_total_$", "net_avg_$", "hit_target", "hit_stop", "timed_out"]); wr.writerows(rows)
    print(open("backtest_results.csv").read())

LOG = "paper_log.csv"
HDR = ["open_time","coin","entry","entry_spread_pct","tp","sl","status","exit_time","exit","exit_spread_pct","reason","net_$","total_cost_pct"]
def read_log(): return list(csv.DictReader(open(LOG))) if os.path.exists(LOG) else []
def ts(s): return int(datetime.fromisoformat(s).timestamp())

def live():
    rows = read_log(); now = int(time.time())
    iso = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    for coin in watchlist():
        try:
            cs = candles(coin); tk = get(f"https://api.exchange.coinbase.com/products/{coin}-USD/ticker"); time.sleep(0.3)
        except Exception as e: print(coin, "skip", e); continue
        bid, ask = float(tk["bid"]), float(tk["ask"]); spr = (ask-bid)/((ask+bid)/2)*100
        mine = [r for r in rows if r["coin"] == coin]
        for r in mine:                                   # manage open paper buy using candle highs/lows since entry
            if r["status"] != "OPEN": continue
            t0 = ts(r["open_time"]); e, tpp, slp = float(r["entry"]), float(r["tp"]), float(r["sl"])
            since = [c for c in cs if c["t"] >= t0 // G * G]
            why = px = None
            for c in since:
                if c["lo"] <= slp: why, px = "stop", slp; break
                if c["hi"] >= tpp: why, px = "target", tpp; break
            if not why and now - t0 >= MAX_HOLD*G: why, px = "time", bid
            if why:
                xp = px if why != "time" else bid
                r.update(status="CLOSED", exit_time=iso(), exit=xp, exit_spread_pct=round(spr, 3), reason=why,
                         **{"net_$": round(net(e, xp, why == "target"), 4), "total_cost_pct": round(float(r["entry_spread_pct"]) + (0 if why == "target" else spr) + (FEE + (MAKER if why == "target" else FEE))*100, 3)})
        busy = any(r["status"] == "OPEN" or now - ts(r["open_time"]) < COOLDOWN for r in mine)
        if not busy and len(cs) > 22 and now - cs[-1]["t"] <= 2*G and signal(cs, len(cs)-1):
            rows.append({"open_time": iso(), "coin": coin, "entry": ask, "entry_spread_pct": round(spr, 3),
                         "tp": round(ask*(1+TP), 8), "sl": round(ask*(1-SL), 8), "status": "OPEN"})
    with open(LOG, "w", newline="") as f:
        w = csv.DictWriter(f, HDR); w.writeheader(); w.writerows([{k: r.get(k, "") for k in HDR} for r in rows])

if __name__ == "__main__":
    {"backtest": backtest, "live": live}[sys.argv[1] if len(sys.argv) > 1 else "live"]()