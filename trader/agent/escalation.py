"""Escalation policy — what deserves a judgment call.

The token-minimization contract, in the human partner's words: the harness
should only boil up things that are a judgment call. So this module is
code deciding *whether a question exists at all*. Most moments produce no
invocation: the rules engine trades the plan, the risk gate enforces the
limits, and none of that needs a model.

What escalates, and to which tier:

- Scheduled slots (always judgment): the 09:35 day plan, the optional
  12:30 revision, the 16:10 post-mortem. These are the strategist's whole
  day; everything else must earn its way up.
- Events, only when code can't resolve them: wire and board disagreeing
  on a held symbol, a losing streak, repeated poll failures, a
  high-confidence hunch that was wrong. Each has a cooldown and a dedup
  key so one condition can't burn budget by re-asking all day.

Everything else — every alert, every fill, every quiet cycle — stays
below the waterline by design.
"""
from __future__ import annotations

from dataclasses import dataclass, field

TIER_TRIAGE = "triage"          # Sonnet: cheap, bounded questions
TIER_STRATEGIST = "strategist"  # Fable: the expensive judgment calls

# One escalation per (kind, symbol) per this many polls.
DEFAULT_COOLDOWN_POLLS = 60     # ~1 hour at 1-minute polls


@dataclass
class Escalation:
    kind: str
    tier: str
    reason: str
    dedup_key: str
    topics: list[str] = field(default_factory=list)


def scheduled_slot(slot: str) -> Escalation:
    """The three standing judgment calls. Not conditional — they exist
    because AGENTS.md says the strategist plans, revises, and reflects."""
    slots = {
        "plan": Escalation("day_plan", TIER_STRATEGIST,
                           "scheduled 09:35 day plan", "slot:plan"),
        "midday": Escalation("midday_review", TIER_STRATEGIST,
                             "scheduled 12:30 plan revision", "slot:midday"),
        "postmortem": Escalation("post_mortem", TIER_STRATEGIST,
                                 "scheduled 16:10 post-mortem", "slot:postmortem"),
    }
    if slot not in slots:
        raise ValueError(f"unknown slot {slot!r}")
    return slots[slot]


def evaluate_events(snapshot: dict, recent_days: list[dict],
                    poll_failures: int = 0) -> list[Escalation]:
    """Event-driven escalations. Pure function: same inputs, same answer.
    Returns the judgment calls the current situation raises — the caller
    applies cooldowns/dedup before spending anything."""
    out: list[Escalation] = []
    account = snapshot.get("account", {})
    held = {p["symbol"] for p in account.get("positions", [])
            if p.get("quantity")}

    # Wire and board disagree about a symbol we HOLD: someone is wrong,
    # and which one is a reading-comprehension question, not a rule.
    news_summary = ((snapshot.get("news") or {}).get("summary") or {})
    for symbol in sorted(held):
        ns = news_summary.get(symbol) or {}
        wire, board = ns.get("wire_sentiment", 0), ns.get("board_sentiment", 0)
        if wire and board and (wire > 0) != (board > 0):
            out.append(Escalation(
                "sentiment_divergence", TIER_TRIAGE,
                f"wire ({wire:+d}) and board ({board:+d}) disagree on held {symbol}",
                f"divergence:{symbol}", topics=[symbol]))

    # Three consecutive losing days is a pattern question, not a day question.
    traded = [d for d in recent_days if d.get("kind") == "trading_day"]
    streak = 0
    for day in reversed(traded):
        if (day.get("pnl_pct") or 0) < 0:
            streak += 1
        else:
            break
    if streak >= 3:
        out.append(Escalation(
            "losing_streak", TIER_STRATEGIST,
            f"{streak} consecutive losing days — is the plan wrong or the market?",
            f"streak:{streak}"))

    # Infrastructure doubt: if the data can't be trusted, that outranks
    # every trading question. Triage decides between HALT and carry on.
    if poll_failures >= 3:
        out.append(Escalation(
            "data_staleness", TIER_TRIAGE,
            f"{poll_failures} consecutive poll failures — data may be stale",
            "staleness"))

    return out


def apply_cooldowns(escalations: list[Escalation], fired: dict,
                    now_poll: int, cooldown: int = DEFAULT_COOLDOWN_POLLS
                    ) -> list[Escalation]:
    """Drop escalations whose dedup_key fired within the cooldown window.
    `fired` maps dedup_key -> poll index when it last fired; the caller
    owns persisting it. Approved escalations are recorded into `fired`."""
    approved: list[Escalation] = []
    for esc in escalations:
        last = fired.get(esc.dedup_key)
        if last is not None and (now_poll - last) < cooldown:
            continue
        fired[esc.dedup_key] = now_poll
        approved.append(esc)
    return approved
