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
    for need in ("scanner_log_v4.csv", "history", "latest.json"):
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

STATE = HERE / "selfcheck_state.json"   # kept between runs by the Actions cache, never committed
try:
    last_alert = json.loads(STATE.read_text()).get("last_alert", 0)
except Exception:
    last_alert = 0

if not problems:
    print("OK: scanner healthy")
    STATE.write_text(json.dumps({"last_alert": 0}))      # healthy again: next failure alerts at once
else:
    print("PROBLEMS:\n- " + "\n- ".join(problems))
    topic = os.environ.get("NTFY_TOPIC")
    # alert on the first failing check, then repeat at most every 15 minutes while it stays broken
    if topic and now - last_alert > 15 * 60:
        try:
            import requests
            requests.post(f"https://ntfy.sh/{topic}", data="\n".join(problems).encode(),
                          headers={"Title": "Scanner problem", "Priority": "high", "Tags": "warning"}, timeout=15)
            STATE.write_text(json.dumps({"last_alert": now}))
        except Exception as e:
            print("ntfy failed:", e)
sys.exit(0)
