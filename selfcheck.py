"""Health check for the cscan scanner. Run every 5 minutes; prints problems and, if any,
sends one ntfy push (repeated at most every ~30 min while it stays broken).
Exit code is always 0 so the workflow itself never looks failed."""
import csv, json, os, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
MAX_AGE = 12 * 60          # scan older than this = problem
problems = []
now = time.time()


def ts(s):
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


# 1. latest scan freshness
try:
    last = ts(json.loads((HERE / "latest.json").read_text())["scan_utc"])
    age = now - last
    if age > MAX_AGE:
        problems.append(f"Last scan is {age/60:.0f} min old")
except Exception as e:
    problems.append(f"latest.json unreadable: {e}")

# 2. last commit freshness (catches failed saves/pushes)
try:
    ct = int(subprocess.check_output(["git", "log", "-1", "--format=%ct"], cwd=HERE, text=True).strip())
    if now - ct > MAX_AGE:
        problems.append(f"Last commit is {(now-ct)/60:.0f} min old")
except Exception as e:
    problems.append(f"git log failed: {e}")

# 3. scripts compile
for f in sorted(HERE.glob("*.py")):
    r = subprocess.run([sys.executable, "-I", "-m", "py_compile", str(f)], capture_output=True, text=True)
    if r.returncode:
        problems.append(f"{f.name} does not compile")

# 4. workflow still saves the log files (this broke once)
try:
    yml = (HERE / ".github/workflows/scan.yml").read_text()
    for need in ("scanner_log_v4.csv", "history", "logs", "latest.json", "fill_log.csv", "paper_log.csv"):
        if need not in yml:
            problems.append(f"scan.yml no longer saves {need}")
except Exception as e:
    problems.append(f"scan.yml unreadable: {e}")

# 5. log rows exist for recent scans (history says flagged coins, CSV must have them)
try:
    hist = HERE / "history" / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"
    recs = [json.loads(l) for l in hist.read_text().splitlines() if l.strip()][-12:]
    have = set()
    with (HERE / "scanner_log_v4.csv").open() as f:
        rd = csv.reader(f); next(rd, None)
        for row in rd:
            have.add(row[0][:16])
    missing = [r["t"] for r in recs if r.get("flag") and r["t"] not in have]
    if missing:
        problems.append(f"{len(missing)} recent scan(s) missing from scanner_log_v4.csv ({missing[0][11:]}...)")
    if recs and now - ts(recs[-1]["t"]) > MAX_AGE + 5 * 60:
        problems.append("history file has no recent entry")
except FileNotFoundError:
    pass  # new day, no history yet
except Exception as e:
    problems.append(f"log check failed: {e}")

# ---- 6. programming: the scanner's own rules still behave as designed
KNOWN = {"REAL UP", "REAL DOWN", "UP vs TREND", "DOWN vs TREND", "FADING", "CHATTER",
         "THIN PUMP", "THIN DROP", "UP NO VOL", "DOWN NO VOL", "WATCH"}
try:
    import math
    rows = json.loads((HERE / "latest.json").read_text())["rows"]
    seen = set()
    for r in rows:
        a = r.get("asset")
        if a in seen:
            problems.append(f"{a} appears twice in the latest scan")
        seen.add(a)
        if r.get("label") not in KNOWN:
            problems.append(f"{a}: unknown label {r.get('label')}")
        px, ret = r.get("price"), r.get("ret_15m")
        if px is None or not math.isfinite(px) or px <= 0:
            problems.append(f"{a}: bad price {px}")
        if ret is None or not math.isfinite(ret) or abs(ret) > 1:
            problems.append(f"{a}: bad 15m move {ret}")
        if r.get("label") == "REAL UP" and not (ret >= 0.01 and (r.get("vol_x") or 0) >= 2):
            problems.append(f"{a}: REAL UP but move/volume below the rule")
        if r.get("label") == "REAL UP" and not r.get("cb"):
            problems.append(f"{a}: REAL UP but not tradable on Coinbase")
except Exception as e:
    problems.append(f"latest.json rule check failed: {e}")

try:   # classify() on made-up coins: known answers
    from types import SimpleNamespace
    sys.path.insert(0, str(HERE))
    import crypto_movement_scanner as sc
    A = SimpleNamespace(min_liq=5e6, move_pct=1.0, small_move_pct=2.0, vol_x=2.0, small_vol_x=4.0,
                        watch_move_pct=0.5, watch_vol_x=1.5)
    def coin(ret, vx, liq):
        return {"cb": {"ret_15m": ret, "ret_1h": ret, "vol_z": 3, "vol_x": vx, "rising": ret > 0,
                       "quote_vol_24h": liq, "price": 1.0, "pct_24h": 1.0}}
    cases = [("big coin +2% 3x volume", coin(.02, 3, 5e7), "REAL UP"),
             ("big coin -2% 3x volume", coin(-.02, 3, 5e7), "REAL DOWN"),
             ("big coin +2% weak volume", coin(.02, 1, 5e7), "UP NO VOL"),
             ("small coin +1.5% 5x volume", coin(.015, 5, 1e6), None),
             ("small coin +3% 5x volume", coin(.03, 5, 1e6), "THIN PUMP"),
             ("quiet coin", coin(.001, 1, 5e7), None)]
    for name, c, want in cases:
        got = sc.classify(c, A)
        got = got[0] if got else None
        if want is None and got in (None, "WATCH"):
            continue
        if got != want:
            problems.append(f"classify test failed: {name}: got {got}, expected {want}")
except Exception as e:
    problems.append(f"classify test could not run: {e}")

# ---- 7. theory: did past REAL UP signals pay after fees? (printed every run; pushed rarely)
FEE = 0.012
theory = None
try:
    px = {}
    sigs = []
    for lf in sorted((HERE / "logs").glob("scanner_log_*.csv")) + [HERE / "scanner_log_v4.csv"]:
        with lf.open() as f:
            for r in csv.DictReader(f):
                t = ts(r["ts_utc"]); p = float(r["price"])
                px.setdefault(r["asset"], []).append((t, p))
                if r["label"] == "REAL UP":
                    sigs.append((t, r["asset"], p))
    for hf in (HERE / "history").glob("*.jsonl"):
        for l in hf.read_text().splitlines():
            if not l.strip():
                continue
            d = json.loads(l); t = ts(d["t"])
            for x in d.get("flag", []):
                px.setdefault(x["asset"], []).append((t, x["price"]))
            for a, p in (d.get("px") or {}).items():
                px.setdefault(a, []).append((t, p))
    sigs.sort(); first = {}; ind = []
    for t, a, p in sigs:                     # one signal per coin per hour
        if a in first and t - first[a] < 3600:
            continue
        first[a] = t; ind.append((t, a, p))
    res = []
    for t, a, p0 in ind:
        if now - t < 40 * 60:
            continue                          # not old enough to score yet
        c = [p for tt, p in px.get(a, []) if abs(tt - (t + 1800)) <= 150]
        if c:
            res.append(c[0] / p0 - 1 - FEE)
    if res:
        n = len(res); mean = sum(res) / n; win = sum(x > 0 for x in res) / n
        theory = (n, mean, win)
        print(f"THEORY: {n} scored REAL UP signals, 30-min result after fees: mean {mean*100:+.2f}%, "
              f"profitable {win*100:.0f}% (needs many separate events to mean much)")
    else:
        print("THEORY: no scored REAL UP signals yet")
except Exception as e:
    print("theory check failed:", e)

STATE = HERE / "selfcheck_state.json"   # kept between runs by the Actions cache, never committed
try:
    last_alert = json.loads(STATE.read_text()).get("last_alert", 0)
except Exception:
    last_alert = 0

try:
    last_theory = json.loads(STATE.read_text()).get("last_theory", 0)
except Exception:
    last_theory = 0
if theory and theory[0] >= 10 and theory[1] < -0.02 and now - last_theory > 6 * 3600:
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            import requests
            requests.post(f"https://ntfy.sh/{topic}", headers={"Title": "Signals losing money", "Tags": "chart_with_downwards_trend"},
                          data=f"Last {theory[0]} REAL UP signals: 30-min result after fees {theory[1]*100:+.1f}% on average, {theory[2]*100:.0f}% profitable.".encode(), timeout=15)
            last_theory = now
        except Exception as e:
            print("ntfy failed:", e)

if not problems:
    print("OK: scanner healthy")
    STATE.write_text(json.dumps({"last_alert": 0, "last_theory": last_theory}))      # healthy again: next failure alerts at once
else:
    print("PROBLEMS:\n- " + "\n- ".join(problems))
    topic = os.environ.get("NTFY_TOPIC")
    # alert on the first failing check, then repeat at most every 15 minutes while it stays broken
    if topic and now - last_alert > 15 * 60:
        try:
            import requests
            requests.post(f"https://ntfy.sh/{topic}", data="\n".join(problems).encode(),
                          headers={"Title": "Scanner problem", "Priority": "high", "Tags": "warning"}, timeout=15)
            STATE.write_text(json.dumps({"last_alert": now, "last_theory": last_theory}))
        except Exception as e:
            print("ntfy failed:", e)
sys.exit(0)