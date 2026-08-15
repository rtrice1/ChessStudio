"""Tests for the scheduled-event guard (the calendar blackout edge)."""
import json
import os
import tempfile
import unittest
from datetime import datetime

from agent.events import (ET, Blackout, active_blackouts, entry_blocked,
                          load_events, seed, should_flatten)
from agent.strategist import DayPlan, SessionContext, decide

FOMC = {"ts": "2026-08-19T14:00:00", "kind": "FOMC", "symbol": None,
        "blackout_before_min": 45, "blackout_after_min": 45, "flatten": True}
EARNINGS = {"ts": "2026-08-19T16:05:00", "kind": "earnings", "symbol": "NVDA",
            "blackout_before_min": 60, "blackout_after_min": 0}


def at(hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime(2026, 8, 19, int(h), int(m), tzinfo=ET)


class TestBlackoutWindows(unittest.TestCase):
    def test_inside_window(self):
        for t in ("13:15", "14:00", "14:45"):
            bl = active_blackouts([FOMC], at(t))
            self.assertEqual(len(bl), 1, t)
            self.assertIn("FOMC", bl[0].reason)

    def test_outside_window(self):
        for t in ("13:14", "14:46", "09:30"):
            self.assertEqual(active_blackouts([FOMC], at(t)), [], t)

    def test_symbol_specific_blocks_only_that_name(self):
        bl = active_blackouts([EARNINGS], at("15:30"))
        self.assertEqual(entry_blocked(bl, "NVDA") is not None, True)
        self.assertIsNone(entry_blocked(bl, "AAPL"))

    def test_market_wide_blocks_everything(self):
        bl = active_blackouts([FOMC], at("14:00"))
        self.assertIsNotNone(entry_blocked(bl, "AAPL"))
        self.assertIsNotNone(entry_blocked(bl, "XOM"))

    def test_flatten_only_for_market_wide_flagged(self):
        self.assertIsNotNone(should_flatten(active_blackouts([FOMC], at("14:00"))))
        self.assertIsNone(should_flatten(active_blackouts([EARNINGS], at("15:30"))))

    def test_malformed_events_never_raise(self):
        junk = [{"ts": "not-a-date"}, {"kind": "no ts at all"}, {}]
        self.assertEqual(active_blackouts(junk, at("14:00")), [])

    def test_aware_timestamps_respected(self):
        ev = {**FOMC, "ts": "2026-08-19T18:00:00+00:00"}  # 14:00 ET in UTC
        self.assertEqual(len(active_blackouts([ev], at("14:00"))), 1)


class TestLoadAndSeed(unittest.TestCase):
    def test_load_missing_and_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_events(tmp), [])
            with open(os.path.join(tmp, "events.json"), "w") as f:
                f.write("{not json")
            self.assertEqual(load_events(tmp), [])

    def test_seed_writes_template_and_never_clobbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed(tmp)
            events = load_events(tmp)
            self.assertGreaterEqual(len(events), 3)
            with open(os.path.join(tmp, "events.json"), "w") as f:
                json.dump([FOMC], f)
            self.assertIn("not touching", seed(tmp))
            self.assertEqual(load_events(tmp), [FOMC])


class TestDecideRespectsBlackouts(unittest.TestCase):
    IND = {"rsi14": 55.0, "atr14": 1.0, "vwap": 100.5,
           "range_high": 101.0, "range_low": 99.5}
    QUOTE = {"symbol": "AAPL", "bid": 101.45, "ask": 101.55, "last": 101.5}

    def snap(self, quote=None):
        return {"account": {"equity": 100_000.0, "cash": 100_000.0,
                            "positions": []},
                "quotes": {"AAPL": quote or self.QUOTE},
                "indicators": {"AAPL": self.IND}}

    def test_market_wide_blackout_blocks_entry(self):
        ctx = SessionContext(day_open_equity=100_000.0, plan=DayPlan())
        ctx.blackouts = [Blackout("FOMC", None, True, "FOMC at 14:00 ET")]
        self.assertEqual(decide(self.snap(), ctx), [])

    def test_other_symbol_blackout_does_not_block(self):
        ctx = SessionContext(day_open_equity=100_000.0, plan=DayPlan())
        ctx.blackouts = [Blackout("earnings", "NVDA", False, "NVDA earnings")]
        decisions = decide(self.snap(), ctx)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, "BUY")

    def test_exits_survive_blackout(self):
        ctx = SessionContext(day_open_equity=100_000.0, plan=DayPlan())
        ctx.blackouts = [Blackout("FOMC", None, True, "FOMC at 14:00 ET")]
        snap = self.snap()
        # held, price at the ATR stop -> the exit must still fire
        snap["account"]["positions"] = [{"symbol": "AAPL", "quantity": 100,
                                         "averagePrice": 104.0}]
        decisions = decide(snap, ctx)
        self.assertEqual([d.action for d in decisions], ["SELL"])

    def test_halted_name_takes_no_entry_and_no_exit(self):
        ctx = SessionContext(day_open_equity=100_000.0, plan=DayPlan())
        halted = {**self.QUOTE, "halted": True}
        self.assertEqual(decide(self.snap(halted), ctx), [])
        snap = self.snap(halted)
        snap["account"]["positions"] = [{"symbol": "AAPL", "quantity": 100,
                                         "averagePrice": 104.0}]
        self.assertEqual(decide(snap, ctx), [])


if __name__ == "__main__":
    unittest.main()
