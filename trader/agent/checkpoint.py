"""Where are we? — the setup checkpoint.

Run by whoever (human or Claude instance) picks up the desk on a new
machine. Inspects the actual state on disk — not a checklist someone
forgot to update — and prints what's done and exactly what to do next,
in order. Safe to run any number of times, changes nothing.

    python -m agent.checkpoint            # status + next actions
    python -m agent.checkpoint --tests    # also run the full test suite
"""
from __future__ import annotations

import argparse
import importlib
import os
import subprocess
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _git_head(base: str) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=base, capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown (not a git checkout — tarball is fine too)"


def _jsonl_lines(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def run(base: str) -> tuple[list[str], list[str]]:
    """Returns (status lines, ordered todo lines)."""
    data = os.path.join(base, "data")
    desk = os.path.join(base, "desk_state")
    ok: list[str] = []
    todo: list[str] = []

    ok.append(f"checkout: {_git_head(base)} on "
              f"python {sys.version_info.major}.{sys.version_info.minor}")
    if sys.version_info < (3, 11):
        todo.append("Python >= 3.11 required — install a newer Python first")

    # dependencies
    try:
        importlib.import_module("websocket")
        ok.append("websocket-client installed (streamer available)")
    except ImportError:
        todo.append("pip install -r requirements.txt "
                    "(streamer runs REST-only without it; tzdata needed on Windows)")
    try:
        ZoneInfo("America/New_York")
        ok.append("timezone database present")
    except Exception:
        todo.append("timezone data missing — pip install tzdata (Windows)")

    # event calendar
    ev = os.path.join(data, "events.json")
    if os.path.exists(ev):
        from agent.events import load_events
        events = load_events(data)
        future = []
        for e in events:
            try:
                ts = datetime.fromisoformat(str(e.get("ts", "")))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=ET)
                if ts > datetime.now(ET):
                    future.append((ts, e.get("kind")))
            except ValueError:
                continue
        nxt = min(future)[0].strftime("%a %b %d %H:%M ET") if future else "none"
        ok.append(f"event calendar: {len(events)} events, next: "
                  f"{min(future)[1] if future else '—'} {nxt}")
    else:
        todo.append("no data/events.json — python -m agent.events seed, "
                    "then put real dates in")

    # gut memory
    gut_lines = _jsonl_lines(os.path.join(desk, "day_memory.jsonl"))
    if gut_lines >= 100:
        ok.append(f"gut memory seeded: {gut_lines} remembered days")
    else:
        todo.append(f"gut memory thin ({gut_lines} days) — "
                    "python -m agent.backfill --days 500")

    # overnight rumor scan
    from agent.rumors import latest_scan, for_date
    scan = latest_scan(os.path.join(desk, "rumors.jsonl"))
    target = for_date(datetime.now(ET))
    if scan and scan.get("for_date") == target:
        if scan.get("fetch_errors") and not scan.get("posts_seen"):
            todo.append(f"rumor scan for {target} ran but ALL sources failed "
                        f"({scan['fetch_errors']} fetch errors) — Reddit "
                        "blocks unauthenticated JSON; set REDDIT_CLIENT_ID / "
                        "REDDIT_CLIENT_SECRET (script app at "
                        "reddit.com/prefs/apps) and rerun")
        else:
            ok.append(f"rumor scan for {target} done "
                      f"({scan.get('posts_seen')} posts, "
                      f"{len(scan.get('tickers') or {})} tickers)")
    else:
        todo.append(f"no rumor scan for {target} yet — python -m agent.rumors scan "
                    "(needs open internet; ~10s)")

    # Schwab credentials + tokens
    if os.environ.get("SCHWAB_APP_KEY") and os.environ.get("SCHWAB_APP_SECRET"):
        ok.append("Schwab app credentials in environment")
    else:
        todo.append("export SCHWAB_APP_KEY / SCHWAB_APP_SECRET "
                    "(and SCHWAB_REDIRECT_URI if the app's callback isn't "
                    "https://127.0.0.1) — see deploy/SCHWAB.md")
    tok = os.path.join(data, "schwab_tokens.json")
    if os.path.exists(tok):
        age_days = (datetime.now()
                    - datetime.fromtimestamp(os.path.getmtime(tok))).days
        if age_days >= 6:
            todo.append(f"Schwab refresh token is {age_days}d old (~7d expiry) — "
                        "python -m agent.schwab auth")
        else:
            ok.append(f"Schwab tokens present ({age_days}d old) — "
                      "verify: python -m agent.schwab test")
    else:
        todo.append("no Schwab tokens — python -m agent.schwab auth (or the "
                    "Schwab connection panel on the dashboard), "
                    "then python -m agent.schwab test")

    # kill switch
    if os.path.exists(os.path.join(data, "HALT")):
        todo.append("data/HALT exists — trading is halted; delete it only "
                    "deliberately (human decision)")
    else:
        ok.append("kill switch clear")

    # scheduling
    if os.path.exists("/run/systemd/system"):
        out = subprocess.run(["systemctl", "is-enabled", "trader-poller.service"],
                             capture_output=True, text=True)
        if out.stdout.strip() == "enabled":
            ok.append("systemd units installed (timers will handle the clock)")
        else:
            todo.append("systemd present but units not installed — "
                        "sudo bash deploy/install.sh (or run pieces by hand)")
    else:
        ok.append("no systemd (Windows/WSL-without-systemd): run pieces "
                  "manually per deploy/DEPLOY.md 'Windows stopgap'")

    return ok, todo


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tests", action="store_true",
                    help="also run the full unittest suite (~1 min)")
    args = ap.parse_args()
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    print("=" * 60)
    print("DESK CHECKPOINT —", datetime.now(ET).strftime("%a %b %d %H:%M ET"))
    print("=" * 60)
    ok, todo = run(base)
    print("\nDONE:")
    for line in ok:
        print(f"  [ok] {line}")
    if todo:
        print("\nNEXT, IN ORDER:")
        for i, line in enumerate(todo, 1):
            print(f"  {i}. {line}")
    else:
        print("\nNothing left — the desk is ready. Monday: "
              "python -m agent.run_live --starting-cash 10000")

    if args.tests:
        print("\nrunning the suite...")
        r = subprocess.run([sys.executable, "-m", "unittest", "discover",
                            "-s", "tests"], cwd=base)
        return r.returncode
    print("\n(add --tests to also run the 460-test suite; "
          "read HANDOFF.md for the full picture)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
