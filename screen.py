#!/usr/bin/env python3
"""Reads latest.txt, applies the crypto skill's screening rules, writes verdict.txt."""
import re, sys, json, time, urllib.request
from datetime import datetime, timezone
from statistics import mean

API = "https://api.exchange.coinbase.com/products/{}-USD/candles?granularity={}"

def pct(s): return float(s.replace('%', '').replace(',', ''))

def parse(path):
    lines = [l for l in open(path).read().splitlines() if l.strip()]
    hdr = lines[0]
    ts = re.search(r'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) UTC', hdr).group(1)
    when = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    rows = []
    for l in lines[2:]:
        t = l.split()
        if len(t) < 14: continue
        f = t[-13:]
        rows.append(dict(asset=t[0], label=' '.join(t[1:-13]), r15=pct(f[0]), r1h=pct(f[1]),
                         volZ=float(f[2]), volx=float(f[3]), vol24=float(f[6].replace(',', '')),
                         ch24=pct(f[7]), cb=f[8]))
    return when, rows

def candles(sym, gran):
    req = urllib.request.Request(API.format(sym, gran), headers={'User-Agent': 'cscan'})
    data = json.load(urllib.request.urlopen(req, timeout=20))
    data.sort(key=lambda c: -c[0])           # newest first: [t, low, high, open, close, vol]
    now = time.time()
    return [c for c in data if c[0] + gran <= now]   # drop forming candle

def stats5(sym):
    c = candles(sym, 300)
    if len(c) < 21: return None
    r15 = (c[0][4] / c[3][4] - 1) * 100
    r1h = (c[0][4] / c[12][4] - 1) * 100
    spike = c[0][5] / (mean(x[5] for x in c[1:21]) or 1)
    lo, hi, op, cl = c[0][1], c[0][2], c[0][3], c[0][4]
    rising = cl > op and cl > c[1][4] and hi > lo and cl >= (hi + lo) / 2
    return dict(r15=r15, r1h=r1h, spike=spike, rising=rising)

def trend1h(sym):
    c = candles(sym, 3600)
    return len(c) >= 20 and c[0][4] > mean(x[4] for x in c[:20])

def main():
    when, rows = parse('latest.txt')
    stamp = when.strftime('%H:%M UTC')
    age = (datetime.now(timezone.utc) - when).total_seconds() / 60
    out = []
    if age > 45:
        out.append(f'STALE | scan {stamp} is {age:.0f} min old')
        return finish(out)
    cb = [r for r in rows if r['cb'] == 'Y']
    runners = [r for r in cb if r['ch24'] >= 15]
    short = [r for r in cb if ('REAL UP' in r['label'] and r['vol24'] >= 300_000)
             or (r['r15'] >= 1.0 and r['volZ'] >= 2)]
    short = [r for r in short if r not in runners][:10]
    movers = sorted(rows, key=lambda r: -abs(r['r15']))[:3]
    mv = ', '.join(f"{r['asset']} {r['r15']:+.1f}%" for r in movers)
    rn = ''.join(f" | RUNNER (info only): {r['asset']} {r['ch24']:+.0f}% 24h" for r in runners)
    if not short:
        out.append(f'NO SETUP | scan {stamp} | top movers: {mv}{rn}')
        return finish(out)
    try:
        btc, eth = stats5('BTC'), stats5('ETH')
        both_up = btc and eth and btc['r15'] > 1 and eth['r15'] > 1
        back = f"BTC {btc['r1h']:+.1f}% / ETH {eth['r1h']:+.1f}% 1h" if btc and eth else 'n/a'
        cands, sells, seen = [], [], []
        for r in short:
            s = stats5(r['asset'])
            if not s: continue
            seen.append(f"{r['asset']} {s['r15']:+.1f}% 15m, {s['spike']:.1f}x vol")
            if s['r15'] <= -1.5 and s['spike'] >= 2: sells.append(r['asset'])
            lane1 = r['vol24'] >= 5_000_000
            need_r, need_v = (2.0, 3.0) if lane1 else (3.0, 4.0)
            if s['r15'] >= need_r and s['spike'] >= need_v and s['rising'] and not both_up and trend1h(r['asset']):
                cands.append((s['r15'] * s['spike'], r, s, 1 if lane1 else 2))
        sellnote = ''.join(f" | SELLOFF: {a}" for a in sells)
        if cands:
            cands.sort(key=lambda x: (x[3], -x[0]))
            _, r, s, lane = cands[0]
            tag = 'BUY CANDIDATE' if lane == 1 else 'BUY CANDIDATE (SMALL COIN, higher risk)'
            out.append(f"{tag} | {r['asset']} | scan {stamp} | {s['r15']:+.1f}% 15m, {s['spike']:.1f}x volume, "
                       f"1h trend up, 24h vol ${r['vol24']/1e6:.1f}M | backdrop {back}{sellnote}")
        else:
            out.append(f"NO SETUP | scan {stamp} | shortlist: {'; '.join(seen) or mv} | backdrop {back}{rn}{sellnote}")
    except Exception as e:
        out.append(f'ERROR | scan {stamp} | candle fetch failed: {e}')
    finish(out)

def finish(out):
    text = '\n'.join(out) + '\n'
    open('verdict.txt', 'w').write(text)
    print(text)

if __name__ == '__main__':
    main()
