"""Make phone.txt: a narrow, easy-to-read version of latest.json for a phone screen.
Reads latest.json only; does not change the scanner."""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    CT = ZoneInfo("America/Chicago")
except Exception:
    CT = timezone(timedelta(hours=-6))

HERE = Path(__file__).parent
data = json.loads((HERE / "latest.json").read_text())
rows = data.get("rows", [])

ts = datetime.fromisoformat(data["scan_utc"]).astimezone(CT)
time_str = ts.strftime("%a %-I:%M %p CT")

SECTIONS = [
    ("🟢 BUY SIGNALS", ["REAL UP"]),
    ("🔴 REAL DROPS", ["REAL DOWN"]),
    ("🟡 AGAINST TREND / FADING", ["UP vs TREND", "DOWN vs TREND", "FADING"]),
    ("💬 CHATTER", ["CHATTER"]),
    ("⚪ THIN (low volume)", ["THIN PUMP", "THIN DROP"]),
]


def pct(x):
    return f"{x * 100:+.1f}%" if x is not None else "  ?"


def money(x):
    if not x:
        return "$0"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if x >= div:
            v = x / div
            return f"${v:.0f}{suf}" if v >= 10 else f"${v:.1f}{suf}"
    return f"${x:.0f}"


def coin(r):
    cb = "on CB" if r.get("cb") else "not CB"
    line1 = f"{r['asset']:<6} 15m {pct(r['ret_15m']):>6}  1h {pct(r['ret_1h']):>6}"
    line2 = f"   vol {r.get('vol_x', 0):.1f}x · {money(r.get('liq'))} · {cb}"
    return [line1, line2]


buys = sum(r["label"] == "REAL UP" for r in rows)
out = [f"CRYPTO SCAN", time_str,
       f"{len(rows)} flagged · {buys} buy signal{'s' if buys != 1 else ''}", ""]

for title, labels in SECTIONS:
    group = [r for r in rows if r["label"] in labels]
    if not group and title.startswith("🟢"):
        out += [title, "  none right now", ""]
        continue
    if not group:
        continue
    out.append(f"{title} ({len(group)})")
    for r in group[:10]:
        out += coin(r)
    if len(group) > 10:
        out.append(f"  +{len(group) - 10} more")
    out.append("")

watch = [r for r in rows if r["label"] == "WATCH"]
if watch:
    out.append(f"👀 WATCH ({len(watch)})")
    line = " "
    for r in watch:
        if len(line) + len(r["asset"]) + 1 > 30:
            out.append(line)
            line = " "
        line += " " + r["asset"]
    out += [line, ""]

out += ["15m/1h = price change", "vol = volume vs normal", "CB = tradable on Coinbase"]
(HERE / "phone.txt").write_text("\n".join(out) + "\n", encoding="utf-8")
print("\n".join(out))


# ---------- phone.html: same info, formatted for a phone screen ----------
import html as _h


def chg(x):
    if x is None:
        return '<span class="n">?</span>'
    cls = "up" if x > 0 else "dn" if x < 0 else "n"
    return f'<span class="{cls}">{x * 100:+.1f}%</span>'


def card(r):
    cb = '<b class="cb">Coinbase</b>' if r.get("cb") else "not on Coinbase"
    return (f'<div class="coin"><div class="top"><span class="sym">{_h.escape(r["asset"])}</span>'
            f'<span class="chg"><small>15m</small> {chg(r["ret_15m"])} '
            f'<small>1h</small> {chg(r["ret_1h"])}</span></div>'
            f'<div class="sub">vol {r.get("vol_x", 0):.1f}x · {money(r.get("liq"))} · {cb}</div></div>')


parts = []
for title, labels in SECTIONS:
    group = [r for r in rows if r["label"] in labels]
    if not group and not title.startswith("🟢"):
        continue
    body = "".join(card(r) for r in group[:10]) or '<div class="none">None right now</div>'
    if len(group) > 10:
        body += f'<div class="none">+{len(group) - 10} more</div>'
    cls = " buy" if title.startswith("🟢") and group else ""
    parts.append(f'<section class="{cls}"><h2>{title} <i>{len(group) or ""}</i></h2>{body}</section>')
if watch:
    names = " ".join(_h.escape(r["asset"]) for r in watch)
    parts.append(f'<section><h2>👀 WATCH <i>{len(watch)}</i></h2><div class="watch">{names}</div></section>')

page = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Crypto Scan">
<title>Crypto Scan</title>
<style>
:root{{color-scheme:light dark;--bg:#f2f2f7;--card:#fff;--txt:#111;--dim:#8a8a8e;--up:#1a9e47;--dn:#d93025}}
@media (prefers-color-scheme:dark){{:root{{--bg:#000;--card:#1c1c1e;--txt:#f2f2f7;--dim:#8e8e93;--up:#32d74b;--dn:#ff453a}}}}
body{{margin:0;padding:max(14px,env(safe-area-inset-top)) 14px 30px;background:var(--bg);color:var(--txt);
font:17px -apple-system,system-ui,sans-serif;-webkit-text-size-adjust:100%}}
h1{{font-size:28px;margin:4px 0 2px}}
.meta{{color:var(--dim);font-size:15px}} .meta b{{color:var(--txt)}} .stale{{color:var(--dn)!important}}
section{{background:var(--card);border-radius:14px;padding:12px 14px;margin-top:14px}}
section.buy{{outline:3px solid var(--up)}}
h2{{font-size:17px;margin:0 0 6px}} h2 i{{font-style:normal;color:var(--dim);font-weight:400}}
.coin{{padding:9px 0;border-top:1px solid rgba(128,128,128,.2)}} .coin:first-of-type{{border-top:0}}
.top{{display:flex;justify-content:space-between;align-items:baseline}}
.sym{{font-weight:700;font-size:19px}} .chg{{font-variant-numeric:tabular-nums;font-weight:600}}
.chg small{{color:var(--dim);font-weight:400;font-size:13px;margin-left:6px}}
.up{{color:var(--up)}} .dn{{color:var(--dn)}} .n{{color:var(--dim)}}
.sub{{color:var(--dim);font-size:14px;margin-top:2px}} .cb{{color:var(--txt)}}
.none{{color:var(--dim);padding:4px 0}} .watch{{line-height:1.7;word-spacing:6px}}
.key{{color:var(--dim);font-size:13px;margin-top:18px;line-height:1.5}}
</style></head><body>
<h1>Crypto Scan</h1>
<div class="meta"><b>{time_str}</b> · <span id="ago"></span><br>{len(rows)} flagged · {buys} buy signal{'s' if buys != 1 else ''}</div>
{''.join(parts)}
<div class="key">15m / 1h = price change · vol = volume vs normal<br>Page refreshes itself every minute.</div>
<script>
var t={int(ts.timestamp() * 1000)},a=document.getElementById('ago');
function u(){{var m=Math.round((Date.now()-t)/60000);a.textContent=m<1?'just now':m+' min ago';
a.className=m>15?'stale':''}}u();setInterval(u,15000);
setTimeout(function(){{location.replace(location.pathname+'?t='+Date.now())}},60000);
</script></body></html>"""
(HERE / "phone.html").write_text(page, encoding="utf-8")