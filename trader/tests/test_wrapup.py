"""Tests for the daily wrap-up report."""
import json
import os
import tempfile
import unittest

from agent.wrapup import compose, write


def jl(path, entries):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


DATE = "2026-08-17"
TS = f"{DATE}T15:00:00"


class TestWrapup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, "data")
        self.desk = os.path.join(self.tmp.name, "desk")
        os.makedirs(self.data)
        os.makedirs(self.desk)

    def tearDown(self):
        self.tmp.cleanup()

    def seed_day(self):
        jl(os.path.join(self.data, "ledger.jsonl"), [
            {"ts": TS, "kind": "live_session_start",
             "plan": "3 slots, shares only — chop suspected"},
            {"ts": TS, "kind": "gut_check",
             "hunch": {"suspected_day_type": "chop", "note": "smells like chop"}},
            {"ts": TS, "kind": "event_blackout", "reason": "CPI at 08:30 ET"},
            {"ts": TS, "kind": "fill", "symbol": "AAPL", "action": "BUY",
             "quantity": 10, "order": {"fillPrice": 100.0},
             "rationale": "ORB: x | score +3.50 (adx 30 trending)",
             "slippage": {"vs_mid_total": 0.5, "half_spread": 0.05}},
            {"ts": TS, "kind": "fill", "symbol": "AAPL", "action": "SELL",
             "quantity": 10, "order": {"fillPrice": 103.0},
             "rationale": "inflection exit: +1.4 ATR and decelerating"},
            {"ts": TS, "kind": "risk_reject", "reason": "entry cutoff: 92%"},
        ])
        jl(os.path.join(self.desk, "journal.jsonl"), [
            {"ts": TS, "kind": "trading_day", "pnl": 30.0, "pnl_pct": 0.003,
             "trades": 1, "flat_at_close": True, "day_type": "chop",
             "daily_stop_hit": False,
             "plan": "3 slots, shares only — chop suspected"},
            {"ts": TS, "kind": "note", "tags": ["post-mortem", "llm"],
             "text": "Chop read was right; kept size small."},
        ])
        jl(os.path.join(self.desk, "rumor_grades.jsonl"), [
            {"kind": "rumor_grade", "for_date": DATE, "symbol": "TSLA",
             "mentions": 12, "sentiment": 4, "day_move_pct": 1.8,
             "abs_move_pct": 1.8, "direction_hit": True},
        ])

    def test_wrapup_covers_the_day(self):
        self.seed_day()
        text = compose(self.data, self.desk, DATE)
        self.assertIn("+30.00", text)                       # headline P&L
        self.assertIn("chop suspected", text)               # the plan, quoted
        self.assertIn("CPI at 08:30", text)                 # blackout honored
        self.assertIn("inflection exit", text)              # exit reasoning row
        self.assertIn(">=3", text)                          # entry score band
        self.assertIn("smells like chop", text)             # gut, graded
        self.assertIn("**right**", text)                    # ...and it was right
        self.assertIn("TSLA", text)                         # rumor graded
        self.assertIn("hit", text)
        self.assertIn("Chop read was right", text)          # my own post-mortem
        self.assertIn("Risk gate rejections: 1", text)

    def test_narrative_and_traded_table(self):
        self.seed_day()
        text = compose(self.data, self.desk, DATE)
        self.assertIn("The day, as it unfolded", text)
        self.assertIn("BUY 10 AAPL @ 100.00", text)
        self.assertIn("ORB: x", text)            # the trigger...
        self.assertNotIn("score +3.50", text.split("## How it went")[0])
        # ...without the score tail leaking into the narrative line
        self.assertIn("1× inflection exit", text)
        self.assertIn("What was traded", text)
        self.assertIn("| AAPL | 1 | 10 | 100.00 | 1,000 | 1,030 | +30.00 |",
                      text)

    def test_wrong_gut_call_is_reported_wrong(self):
        self.seed_day()
        # rewrite journal with a different actual day type
        path = os.path.join(self.desk, "journal.jsonl")
        entries = [json.loads(line) for line in open(path)]
        entries[0]["day_type"] = "trend_up"
        with open(path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        text = compose(self.data, self.desk, DATE)
        self.assertIn("wrong (said chop, was trend_up)", text)

    def test_empty_date_yields_honest_stub(self):
        text = compose(self.data, self.desk, "1999-01-01")
        self.assertIn("No session records", text)

    def test_write_persists_under_desk_state(self):
        self.seed_day()
        path = write(self.data, self.desk, DATE)
        self.assertTrue(path.replace(os.sep, "/").endswith(f"wrapups/{DATE}.md"))
        self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
