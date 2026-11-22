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