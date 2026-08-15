"""The Tier-2/Tier-3 invocation harness: judgment calls only, minimal tokens.

Pipeline for every invocation, no exceptions:

    escalation.py decides WHETHER a question exists
      -> focus.py decides WHAT LITTLE context it needs (hard char budget)
        -> a tight schema decides WHAT SHAPE the answer takes
          -> llm.py enforces the daily token budget and makes the call
            -> the result is applied by code (plan file, desk journal)

Without an API key this whole pipeline runs in dry-run: the exact prompts
land in data/invocations/, the ledger records estimated costs, and nothing
is spent. That's the mock-phase posture — measure first, buy tokens later.

Usage (each scheduled slot is one process run, fired by systemd timers):
    python -m agent.harness --slot plan
    python -m agent.harness --slot midday
    python -m agent.harness --slot postmortem
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.client import BrokerClient                      # noqa: E402
from agent.desk import Desk                                # noqa: E402
from agent.escalation import Escalation, scheduled_slot    # noqa: E402
from agent.focus import FocusSession, build_context, items_from_snapshot  # noqa: E402
from agent.gut import Gut                                  # noqa: E402
from agent.llm import LLMClient, TokenLedger               # noqa: E402
from agent.strategist import DayPlan                       # noqa: E402

# Prompt character budgets per invocation kind. These are the "minimal
# overall context" contract made concrete — focus packs the best items it
# can under these, and everything else stays out of mind.
CHAR_BUDGETS = {"day_plan": 6000, "midday_review": 4000,
                "post_mortem": 5000, "sentiment_divergence": 1500,
                "losing_streak": 4000, "data_staleness": 1200}
MAX_TOKENS = {"day_plan": 1500, "midday_review": 1000, "post_mortem": 2000,
              "sentiment_divergence": 300, "losing_streak": 1500,
              "data_staleness": 200}

DAY_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "symbols": {"type": "array", "items": {"type": "string"},
                    "description": "Symbols eligible for entries today; empty = all"},
        "bias_off": {"type": "array", "items": {"type": "string"},
                     "description": "Symbols to take NO entries in today"},
        "per_trade_risk_pct": {"type": "number",
                               "description": "Fraction of equity risked per trade, <= 0.005"},
        "stop_atr": {"type": "number"},
        "target_atr": {"type": "number"},
        "instrument": {"type": "string", "enum": ["shares", "calls"],
                       "description": "Express entries as stock or long 0DTE calls"},
        "max_positions": {"type": "integer",
                          "description": "Concurrent position budget, 1-6. When "
                                         "more signals fire than slots exist, code "
                                         "ranks them by momentum/news/gut score "
                                         "and only the best trade."},
        "rationale": {"type": "string",
                      "description": "One paragraph: why this plan, citing the evidence used"},
    },
    "required": ["symbols", "bias_off", "per_trade_risk_pct",
                 "stop_atr", "target_atr", "instrument", "max_positions",
                 "rationale"],
    "additionalProperties": False,
}

POST_MORTEM_SCHEMA = {
    "type": "object",
    "properties": {
        "journal_note": {"type": "string",
                         "description": "The day's post-mortem for the desk journal"},
        "beliefs": {"type": "array", "items": {
            "type": "object",
            "properties": {"key": {"type": "string"},
                           "value": {"type": "string"},
                           "reason": {"type": "string"}},
            "required": ["key", "value", "reason"],
            "additionalProperties": False},
            "description": "New or revised beliefs, each with its evidence"},
    },
    "required": ["journal_note", "beliefs"],
    "additionalProperties": False,
}

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string",
                    "enum": ["ignore", "wake_strategist", "halt_trading"]},
        "note": {"type": "string", "description": "One sentence of reasoning"},
    },
    "required": ["verdict", "note"],
    "additionalProperties": False,
}

SCHEMAS = {"day_plan": DAY_PLAN_SCHEMA, "midday_review": DAY_PLAN_SCHEMA,
           "post_mortem": POST_MORTEM_SCHEMA,
           "sentiment_divergence": TRIAGE_SCHEMA,
           "losing_streak": POST_MORTEM_SCHEMA, "data_staleness": TRIAGE_SCHEMA}

TASK_LINES = {
    "day_plan": ("Set today's DayPlan. The mechanical engine will trade it; "
                 "you will not be consulted again until midday. Risk gate "
                 "caps apply regardless of what you choose."),
    "midday_review": ("Revise the day plan if the morning demands it; "
                      "otherwise return it unchanged. This is your only "
                      "revision of the day."),
    "post_mortem": ("Write the day's post-mortem for the desk journal, and "
                    "state any belief this day's evidence creates or revises. "
                    "The next instance of you inherits exactly what you write."),
    "sentiment_divergence": ("Wire and message boards disagree on a held "
                             "symbol. Decide: ignore, wake the strategist, "
                             "or halt trading."),
    "losing_streak": ("Multiple consecutive losing days. Diagnose: is the "
                      "plan wrong, or is this variance? Update beliefs "
                      "accordingly."),
    "data_staleness": ("Market data may be stale. Decide: ignore, wake the "
                       "strategist, or halt trading. When in doubt, halt — "
                       "flat and honest beats clever."),
}


def build_prompt(esc: Escalation, snapshot: dict, desk: Desk,
                 hunch: dict | None, session: FocusSession,
                 rumors: dict | None = None) -> str:
    """Assemble the focused prompt for one judgment call."""
    session.start_task(esc.topics, f"task: {esc.kind}")
    if esc.kind in ("sentiment_divergence", "data_staleness"):
        session.deepen("narrow triage question")
        session.deepen("specifics only")

    items = items_from_snapshot(snapshot, desk.load_context(journal_limit=10),
                                hunch, rumors)
    focused = build_context(items, session.state,
                            budget_chars=CHAR_BUDGETS[esc.kind])
    return (f"You are the {esc.tier} of an automated day-trading desk "
            f"(paper account; hard risk limits enforced in code downstream "
            f"of you).\n\nTASK: {TASK_LINES[esc.kind]}\n"
            f"TRIGGER: {esc.reason}\n\n"
            f"CONTEXT (assembled by focus; "
            f"{len(focused['excluded'])} items deliberately excluded):\n"
            f"{focused['text']}\n\n"
            f"Answer in the required JSON shape only.")


def apply_day_plan(result: dict, data_dir: str) -> DayPlan:
    """Turn a schema-valid reply into the DayPlan the engine trades, with
    code-side clamps — the model proposes, code disposes, here too."""
    plan = DayPlan(
        symbols=list(result.get("symbols") or []),
        bias={s: "off" for s in (result.get("bias_off") or [])},
        per_trade_risk_pct=min(0.005, max(0.001, float(result["per_trade_risk_pct"]))),
        stop_atr=min(3.0, max(0.5, float(result["stop_atr"]))),
        target_atr=min(5.0, max(1.0, float(result["target_atr"]))),
        instrument=(result.get("instrument")
                    if result.get("instrument") in ("shares", "calls")
                    else "shares"),
        max_positions=min(6, max(1, int(result.get("max_positions") or 4))),
        rationale=str(result.get("rationale", ""))[:2000],
    )
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "day_plan.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    return plan


def apply_post_mortem(result: dict, desk: Desk) -> None:
    desk.note(str(result.get("journal_note", ""))[:4000],
              tags=["post-mortem", "llm"])
    for belief in (result.get("beliefs") or [])[:5]:
        desk.set_belief(str(belief["key"])[:80], str(belief["value"])[:500],
                        str(belief["reason"])[:500])


def run(esc: Escalation, base_url: str, data_dir: str, desk_dir: str) -> dict | None:
    client = BrokerClient(base_url)
    desk = Desk(desk_dir)
    gut = Gut(os.path.join(desk_dir, "day_memory.jsonl"))
    llm = LLMClient(TokenLedger(os.path.join(data_dir, "token_ledger.jsonl")),
                    dry_run_dir=os.path.join(data_dir, "invocations"))

    latest_path = os.path.join(data_dir, "latest.json")
    if os.path.exists(latest_path):
        with open(latest_path, encoding="utf-8") as f:
            snapshot = json.load(f)
    else:
        snapshot = {"account": client.account(), "quotes": {}, "indicators": {},
                    "alerts": []}

    hunch = None
    days = gut.days()
    if days and days[-1].get("features"):
        hunch = gut.hunch(days[-1]["features"])

    # The overnight rumor board (with its track record) reaches the
    # planning slots only — intraday triage doesn't need last night's chatter.
    rumors = None
    if esc.kind in ("day_plan", "midday_review"):
        from agent.rumors import context as rumors_context
        rumors = rumors_context(desk_dir)

    prompt = build_prompt(esc, snapshot, desk, hunch, FocusSession(), rumors)
    result = llm.invoke(esc.tier, esc.kind, prompt,
                        schema=SCHEMAS[esc.kind],
                        max_tokens=MAX_TOKENS[esc.kind])

    if result is None:
        print(f"{esc.kind}: no result "
              f"({'dry-run' if llm.dry_run else 'refused/denied'}) — "
              f"prompt was {len(prompt)} chars")
        return None

    if esc.kind in ("day_plan", "midday_review"):
        plan = apply_day_plan(result, data_dir)
        print(f"{esc.kind}: plan applied — {plan.rationale[:120]}")
    elif esc.kind in ("post_mortem", "losing_streak"):
        apply_post_mortem(result, desk)
        print(f"{esc.kind}: journaled, {len(result.get('beliefs') or [])} beliefs")
    else:
        print(f"{esc.kind}: verdict={result.get('verdict')} — {result.get('note')}")
        if result.get("verdict") == "halt_trading":
            open(os.path.join(data_dir, "HALT"), "w").close()
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slot", required=True,
                    choices=["plan", "midday", "postmortem"])
    ap.add_argument("--base-url", default="http://127.0.0.1:8788")
    ap.add_argument("--data-dir",
                    default=os.path.join(os.path.dirname(__file__), "..", "data"))
    ap.add_argument("--desk-dir",
                    default=os.path.join(os.path.dirname(__file__), "..", "desk_state"))
    args = ap.parse_args()
    run(scheduled_slot(args.slot), args.base_url, args.data_dir, args.desk_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
