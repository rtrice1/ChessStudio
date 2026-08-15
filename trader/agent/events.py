"""The scheduled-event guard: the cheapest legal edge on the board.

FOMC statements drop at 14:00 ET, CPI at 08:30, earnings after the close —
all pre-announced to the minute. A desk that simply refuses to open new
positions into a known event time has an edge over one that trades through
it, and it costs nothing. This module reads `data/events.json` and answers
one question per cycle: are we inside a blackout window right now?

events.json is a list of objects, human-maintained (or seeded by
`python -m agent.events seed` with the recurring macro schedule):

    [{"ts": "2026-08-20T14:00:00-04:00", "kind": "FOMC",
      "symbol": null,                 # null = market-wide, else one name
      "blackout_before_min": 30, "blackout_after_min": 15,
      "flatten": false}]              # true = also exit into the event

Blackouts block ENTRIES only, in `decide()` — exits always work, the same
principle as every other limit here. A `flatten: true` event additionally
asks the runner to go flat before the timestamp (for events violent enough
that holding through them is a coin toss, not a trade).
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DEFAULT_BEFORE_MIN = 30
DEFAULT_AFTER_MIN = 15


@dataclass
class Blackout:
    """The active restriction, if any, for one moment in time."""
    kind: str
    symbol: str | None      # None = market-wide
    flatten: bool
    reason: str


def load_events(data_dir: str) -> list[dict]:
    path = os.path.join(data_dir, "events.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            events = json.load(f)
        return events if isinstance(events, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _event_time(ev: dict) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(ev.get("ts", "")))
    except ValueError:
        return None
    # A naive timestamp in the file means ET — that's how humans will type it.
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ET)
    return ts


def active_blackouts(events: list[dict], now: datetime) -> list[Blackout]:
    """All blackout windows covering `now`. Malformed events are skipped —
    a typo in the calendar must never crash the trading loop."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    out: list[Blackout] = []
    for ev in events:
        ts = _event_time(ev)
        if ts is None:
            continue
        before = timedelta(minutes=float(ev.get("blackout_before_min",
                                                DEFAULT_BEFORE_MIN)))
        after = timedelta(minutes=float(ev.get("blackout_after_min",
                                               DEFAULT_AFTER_MIN)))
        if ts - before <= now <= ts + after:
            kind = str(ev.get("kind", "event"))
            sym = ev.get("symbol") or None
            out.append(Blackout(
                kind=kind, symbol=sym, flatten=bool(ev.get("flatten")),
                reason=f"{kind} at {ts:%H:%M} ET "
                       f"({'market-wide' if sym is None else sym})"))
    return out


def entry_blocked(blackouts: list[Blackout], symbol: str) -> str | None:
    """The reason this symbol can't take a new entry right now, or None."""
    for b in blackouts:
        if b.symbol is None or b.symbol == symbol:
            return b.reason
    return None


def should_flatten(blackouts: list[Blackout]) -> str | None:
    """A market-wide flatten-flagged event in window means go flat."""
    for b in blackouts:
        if b.flatten and b.symbol is None:
            return b.reason
    return None


# The recurring macro schedule, ET. Dates change; times don't. `seed`
# writes the next few weeks of these so the file starts useful — the
# human (or a later feed) keeps it current.
RECURRING = [
    {"kind": "CPI", "time": "08:30", "blackout_before_min": 15,
     "blackout_after_min": 30, "note": "monthly, ~mid-month; check BLS calendar"},
    {"kind": "FOMC", "time": "14:00", "blackout_before_min": 45,
     "blackout_after_min": 45, "flatten": True,
     "note": "8x/year; check federalreserve.gov calendar"},
    {"kind": "NFP", "time": "08:30", "blackout_before_min": 15,
     "blackout_after_min": 30, "note": "first Friday monthly"},
]


def seed(data_dir: str) -> str:
    """Write a template events.json (never clobbers an existing one)."""
    path = os.path.join(data_dir, "events.json")
    if os.path.exists(path):
        return f"{path} already exists — not touching it"
    os.makedirs(data_dir, exist_ok=True)
    template = [
        {"ts": f"2026-01-01T{r['time']}:00", "kind": r["kind"],
         "symbol": None,
         "blackout_before_min": r["blackout_before_min"],
         "blackout_after_min": r["blackout_after_min"],
         "flatten": r.get("flatten", False),
         "_note": r["note"] + " — REPLACE ts with real dates"}
        for r in RECURRING
    ]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(template, f, indent=2)
    return f"seeded {path} — edit the timestamps to the real calendar"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["seed", "check"])
    ap.add_argument("--data-dir",
                    default=os.path.join(os.path.dirname(__file__), "..", "data"))
    args = ap.parse_args()
    if args.cmd == "seed":
        print(seed(args.data_dir))
    else:
        blackouts = active_blackouts(load_events(args.data_dir),
                                     datetime.now(ET))
        if not blackouts:
            print("no active blackout")
        for b in blackouts:
            print(f"BLACKOUT: {b.reason}"
                  + (" [flatten]" if b.flatten else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
