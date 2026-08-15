"""Tests for the escalation policy, token budgets, and invocation harness."""
import json
import os
import tempfile
import unittest

from agent.escalation import (TIER_STRATEGIST, TIER_TRIAGE, apply_cooldowns,
                              evaluate_events, scheduled_slot)
from agent.harness import (CHAR_BUDGETS, apply_day_plan, apply_post_mortem,
                           build_prompt, scheduled_slot as _slot)
from agent.desk import Desk
from agent.focus import FocusSession
from agent.llm import (DEFAULT_DAILY_CAPS, LLMClient, TokenLedger,
                       estimate_tokens)


def snapshot(positions=None, news_summary=None):
    return {
        "account": {"equity": 100_000.0, "cash": 100_000.0,
                    "positions": positions or []},
        "quotes": {}, "indicators": {}, "alerts": [],
        "news": {"summary": news_summary or {}},
    }


POS_NVDA = {"symbol": "NVDA", "quantity": 50, "averagePrice": 180.0,
            "marketValue": 9000.0}


class TestEscalationPolicy(unittest.TestCase):
    def test_scheduled_slots_exist_and_are_strategist_tier(self):
        for slot in ("plan", "midday", "postmortem"):
            esc = scheduled_slot(slot)
            self.assertEqual(esc.tier, TIER_STRATEGIST)
        with self.assertRaises(ValueError):
            scheduled_slot("lunch")

    def test_quiet_day_escalates_nothing(self):
        self.assertEqual(evaluate_events(snapshot(), []), [])

    def test_divergence_on_held_symbol_escalates_to_triage(self):
        snap = snapshot(positions=[POS_NVDA],
                        news_summary={"NVDA": {"wire_sentiment": 2,
                                               "board_sentiment": -3}})
        escs = evaluate_events(snap, [])
        self.assertEqual(len(escs), 1)
        self.assertEqual(escs[0].tier, TIER_TRIAGE)
        self.assertEqual(escs[0].kind, "sentiment_divergence")
        self.assertIn("NVDA", escs[0].topics)

    def test_divergence_on_unheld_symbol_does_not_escalate(self):
        snap = snapshot(news_summary={"NVDA": {"wire_sentiment": 2,
                                               "board_sentiment": -3}})
        self.assertEqual(evaluate_events(snap, []), [])

    def test_agreeing_sentiment_does_not_escalate(self):
        snap = snapshot(positions=[POS_NVDA],
                        news_summary={"NVDA": {"wire_sentiment": 2,
                                               "board_sentiment": 1}})
        self.assertEqual(evaluate_events(snap, []), [])

    def test_losing_streak_escalates_at_three(self):
        days = [{"kind": "trading_day", "pnl_pct": -0.01} for _ in range(3)]
        escs = evaluate_events(snapshot(), days)
        self.assertEqual([e.kind for e in escs], ["losing_streak"])
        self.assertEqual(escs[0].tier, TIER_STRATEGIST)

    def test_streak_broken_by_winning_day(self):
        days = [{"kind": "trading_day", "pnl_pct": -0.01},
                {"kind": "trading_day", "pnl_pct": 0.002},
                {"kind": "trading_day", "pnl_pct": -0.01},
                {"kind": "trading_day", "pnl_pct": -0.01}]
        self.assertEqual(evaluate_events(snapshot(), days), [])

    def test_poll_failures_escalate(self):
        escs = evaluate_events(snapshot(), [], poll_failures=3)
        self.assertEqual([e.kind for e in escs], ["data_staleness"])

    def test_cooldown_suppresses_repeat(self):
        snap = snapshot(positions=[POS_NVDA],
                        news_summary={"NVDA": {"wire_sentiment": 2,
                                               "board_sentiment": -3}})
        fired = {}
        first = apply_cooldowns(evaluate_events(snap, []), fired, now_poll=10)
        self.assertEqual(len(first), 1)
        again = apply_cooldowns(evaluate_events(snap, []), fired, now_poll=30)
        self.assertEqual(again, [])
        later = apply_cooldowns(evaluate_events(snap, []), fired, now_poll=100)
        self.assertEqual(len(later), 1)


class TestTokenBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = TokenLedger(os.path.join(self.tmp.name, "tokens.jsonl"))

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, caps=None):
        return LLMClient(self.ledger, caps=caps, dry_run=True,
                         dry_run_dir=os.path.join(self.tmp.name, "inv"))

    def test_dry_run_records_and_returns_none(self):
        llm = self.client()
        self.assertTrue(llm.dry_run)
        result = llm.invoke("strategist", "day_plan", "plan the day", max_tokens=1000)
        self.assertIsNone(result)
        entries = self.ledger.entries()
        self.assertEqual(entries[-1]["status"], "dry_run")
        self.assertEqual(entries[-1]["model"], "claude-fable-5")
        files = os.listdir(os.path.join(self.tmp.name, "inv"))
        self.assertEqual(len(files), 1)
        with open(os.path.join(self.tmp.name, "inv", files[0])) as f:
            saved = json.load(f)
        self.assertEqual(saved["prompt"], "plan the day")

    def test_budget_cap_is_hard(self):
        llm = self.client(caps={"strategist": 1400, "triage": 0, "watcher": 0})
        self.assertIsNone(llm.invoke("strategist", "day_plan", "x", max_tokens=1000))
        # dry-run charged 500 estimated output tokens; next 1000 exceeds 1400
        self.assertIsNone(llm.invoke("strategist", "day_plan", "x", max_tokens=1000))
        statuses = [e["status"] for e in self.ledger.entries()]
        self.assertEqual(statuses, ["dry_run", "budget_denied"])

    def test_budgets_are_per_tier(self):
        llm = self.client(caps={"strategist": 100, "triage": 5000, "watcher": 0})
        self.assertIsNone(llm.invoke("strategist", "day_plan", "x", max_tokens=500))
        llm.invoke("triage", "sentiment_divergence", "y", max_tokens=300)
        summary = self.ledger.summary_today()
        self.assertEqual(summary["refused"], 1)
        self.assertEqual(summary["invocations"], 1)

    def test_unknown_tier_raises(self):
        with self.assertRaises(ValueError):
            self.client().invoke("ceo", "day_plan", "x")

    def test_cost_estimation(self):
        self.ledger.record("ok", "strategist", "day_plan", 1_000_000, 100_000)
        entry = self.ledger.entries()[-1]
        self.assertAlmostEqual(entry["est_cost_usd"], 10.0 + 5.0, places=4)

    def test_estimate_tokens_order_of_magnitude(self):
        self.assertEqual(estimate_tokens("abcd" * 100), 100)

    def test_default_caps_are_tight(self):
        self.assertLessEqual(DEFAULT_DAILY_CAPS["strategist"], 50_000)


class TestHarnessPromptsAndApply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.desk = Desk(os.path.join(self.tmp.name, "desk"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_prompt_respects_char_budget(self):
        self.desk.note("x" * 3000, tags=["big"])
        esc = _slot("plan")
        prompt = build_prompt(esc, snapshot(positions=[POS_NVDA]), self.desk,
                              None, FocusSession())
        self.assertLess(len(prompt), CHAR_BUDGETS["day_plan"] + 1500)
        self.assertIn("TASK:", prompt)
        self.assertIn("JSON", prompt)

    def test_apply_day_plan_clamps_model_output(self):
        result = {"symbols": ["NVDA"], "bias_off": ["TSLA"],
                  "per_trade_risk_pct": 0.5,   # model asked for 50% — clamp
                  "stop_atr": 0.01, "target_atr": 99.0,
                  "max_positions": 40,         # model asked for 40 names — clamp
                  "rationale": "r" * 5000}
        plan = apply_day_plan(result, self.tmp.name)
        self.assertEqual(plan.per_trade_risk_pct, 0.005)
        self.assertEqual(plan.stop_atr, 0.5)
        self.assertEqual(plan.target_atr, 5.0)
        self.assertEqual(plan.max_positions, 6)
        self.assertEqual(plan.bias, {"TSLA": "off"})
        # A plan file written before the budget existed still loads sanely.
        del result["max_positions"]
        self.assertEqual(apply_day_plan(result, self.tmp.name).max_positions, 4)
        self.assertEqual(len(plan.rationale), 2000)
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "day_plan.json")))

    def test_apply_post_mortem_journals_and_caps_beliefs(self):
        result = {"journal_note": "the day in review",
                  "beliefs": [{"key": f"b{i}", "value": "v", "reason": "r"}
                              for i in range(10)]}
        apply_post_mortem(result, self.desk)
        notes = self.desk.journal_entries(kind="note")
        self.assertEqual(notes[-1]["text"], "the day in review")
        self.assertEqual(len(self.desk.beliefs()), 5)  # capped at 5


if __name__ == "__main__":
    unittest.main()
