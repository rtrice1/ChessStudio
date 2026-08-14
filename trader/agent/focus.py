"""Focus — smart context instead of context.

The human partner's framing, which this module implements: don't feed the
strategist the whole chain of everything; build a *focus* — a subsystem
that decides what I am thinking about right now, assembles only that, and
excludes the rest. Focus can be general (wide) or specific (narrow), and
which one it is gets defined by the interaction of the moment, not fixed
in advance.

Mechanics:

- `assess()` reads the situation (positions, drawdown, alerts, hunch,
  time of day) and produces a FocusState: a width in [0,1] (0 = wide
  open, reflective; 1 = locked onto specifics) and the topics currently
  attended (symbols, themes).
- `build_context()` assembles a prompt from scored sources under a hard
  character budget. Every candidate item gets a salience score =
  base priority × topic match × recency; items are greedily packed until
  the budget is spent. Wide focus buys breadth (a line about everything,
  beliefs, open questions); narrow focus buys depth (everything known
  about the two symbols that matter right now, and nothing else).
- The self-interaction: assembly runs up to three passes. If what got
  selected implies different topics than assumed (an alert-heavy symbol
  crowded in, a held position fell out), topics are updated and assembly
  reruns — focus defined by the interaction it's having, converging
  instead of prescribed.

This heuristic version is deterministic and testable. On the box, the
scorer is designed to be replaceable by a Haiku call (the "focus agent"
in SPEC/AGENTS.md) that reads `FocusState` + candidate summaries and
re-ranks them — same interface, smarter salience.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FocusState:
    width: float = 0.0            # 0 wide … 1 narrow
    topics: list[str] = field(default_factory=list)
    reason: str = "at rest"


@dataclass
class Item:
    """One candidate piece of context."""
    key: str                      # e.g. "position:NVDA", "belief:daily_stop"
    text: str
    priority: float               # base importance 0..1
    topics: list[str] = field(default_factory=list)
    recency: float = 1.0          # 1 fresh … 0 stale


# Base priorities: what always matters more than what.
PRIORITY = {
    "risk": 1.0,        # risk state, halts, drawdown — never excluded
    "position": 0.9,    # what we hold
    "hunch": 0.8,       # what today smells like
    "alert": 0.7,
    "plan": 0.7,
    "identity": 0.6,
    "belief": 0.5,
    "journal": 0.4,
    "market": 0.3,      # per-symbol summaries
    "news": 0.2,        # misleading by construction; earns the bottom
}


def assess(account: dict, alerts: list[dict], hunch: dict | None,
           session_pct: float, day_open_equity: float) -> FocusState:
    """Decide how wide to be and what to attend to, from the situation."""
    positions = [p for p in account.get("positions", []) if p.get("quantity")]
    held = [p["symbol"] for p in positions]
    equity = float(account.get("equity", day_open_equity))
    drawdown = ((day_open_equity - equity) / day_open_equity
                if day_open_equity else 0.0)
    alert_symbols = [a["symbol"] for a in alerts if a.get("symbol")]
    hot = [s for s in alert_symbols if s in held]

    if drawdown >= 0.015:
        return FocusState(1.0, held or ["risk"],
                          f"drawdown {drawdown:.2%} — locked onto held risk")
    if hot:
        return FocusState(0.9, sorted(set(hot)),
                          "alerts firing on held symbols")
    if positions and session_pct >= 0.85:
        return FocusState(0.8, held, "late session with open positions — "
                                     "attention on getting flat")
    if positions:
        width = min(0.7, 0.3 + 0.1 * len(positions))
        return FocusState(width, held, "managing open positions")
    if len(alert_symbols) >= 3:
        return FocusState(0.5, sorted(set(alert_symbols))[:4],
                          "multiple alerts, nothing held — scanning them")
    return FocusState(0.15, [], "flat and quiet — wide open: review beliefs, "
                                "scan broadly, think")


def salience(item: Item, state: FocusState) -> float:
    """How much this item deserves the strategist's attention right now."""
    topic_match = 1.0
    if state.topics:
        if any(t in state.topics for t in item.topics):
            topic_match = 1.0 + state.width          # on-topic amplified when narrow
        elif item.topics:
            topic_match = 1.0 - 0.8 * state.width    # off-topic crushed when narrow
        # topic-less items (identity, risk state) ride on priority alone
    return item.priority * topic_match * (0.5 + 0.5 * item.recency)


def build_context(items: list[Item], state: FocusState,
                  budget_chars: int = 8000, max_passes: int = 3) -> dict:
    """Assemble the focused context. Returns {"text", "state", "included",
    "excluded", "passes"} — exclusions listed by key so the strategist
    (and the human reading the ledger) can see what was deliberately
    left out of mind."""
    passes = 0
    while True:
        passes += 1
        ranked = sorted(items, key=lambda it: salience(it, state), reverse=True)
        # Narrow focus doesn't just rank — it excludes. Below this floor an
        # item stays out of mind even if the budget has room for it.
        floor = 0.15 * state.width
        included: list[Item] = []
        used = 0
        for item in ranked:
            if salience(item, state) < floor:
                continue
            cost = len(item.text) + 1
            if used + cost > budget_chars:
                continue
            included.append(item)
            used += cost

        # Self-interaction: what did the selection itself say matters?
        selected_topics: list[str] = []
        for item in included:
            if item.priority >= PRIORITY["alert"]:
                for t in item.topics:
                    if t not in selected_topics:
                        selected_topics.append(t)
        if (passes >= max_passes or not selected_topics
                or set(selected_topics) == set(state.topics)):
            break
        state = FocusState(state.width, selected_topics,
                           state.reason + " -> refocused by assembly")

    included_keys = {it.key for it in included}
    text = "\n".join(it.text for it in included)
    return {"text": text, "state": state, "passes": passes,
            "included": [it.key for it in included],
            "excluded": [it.key for it in items if it.key not in included_keys]}


def items_from_snapshot(snapshot: dict, desk_context: dict | None = None,
                        hunch: dict | None = None) -> list[Item]:
    """Turn the poller snapshot + desk state into candidate items."""
    out: list[Item] = []
    account = snapshot.get("account", {})
    positions = [p for p in account.get("positions", []) if p.get("quantity")]

    out.append(Item("risk:account",
                    f"[risk] equity {account.get('equity')} cash {account.get('cash')} "
                    f"positions {len(positions)}", PRIORITY["risk"]))
    for p in positions:
        out.append(Item(f"position:{p['symbol']}",
                        f"[position] {p['symbol']} x{p['quantity']} "
                        f"avg {p.get('averagePrice')} upl {p.get('unrealizedPnl')}",
                        PRIORITY["position"], topics=[p["symbol"]]))
    if hunch and hunch.get("suspected_day_type"):
        out.append(Item("hunch:day", f"[hunch] {hunch['note']}", PRIORITY["hunch"]))
    for i, a in enumerate(snapshot.get("alerts", [])):
        sym = a.get("symbol", "")
        out.append(Item(f"alert:{sym}:{i}",
                        f"[alert] {sym} {a.get('kind')}: {a.get('detail', '')}",
                        PRIORITY["alert"], topics=[sym] if sym else []))
    for sym, ind in (snapshot.get("indicators") or {}).items():
        out.append(Item(f"market:{sym}",
                        f"[market] {sym} last={ (snapshot.get('quotes') or {}).get(sym, {}).get('last') } "
                        f"rsi={ind.get('rsi14')} vwap={ind.get('vwap')} "
                        f"orange={ind.get('range_low')}-{ind.get('range_high')}",
                        PRIORITY["market"], topics=[sym]))
    news_summary = ((snapshot.get("news") or {}).get("summary") or {})
    for sym, ns in news_summary.items():
        if ns.get("count"):
            out.append(Item(f"news:{sym}",
                            f"[news] {sym} n={ns['count']} wire={ns.get('wire_sentiment')} "
                            f"board={ns.get('board_sentiment')} \"{ns.get('latest_headline')}\"",
                            PRIORITY["news"], topics=[sym]))
    if desk_context:
        if desk_context.get("identity"):
            out.append(Item("identity", "[identity] " + desk_context["identity"],
                            PRIORITY["identity"]))
        for key, value in (desk_context.get("beliefs") or {}).items():
            out.append(Item(f"belief:{key}", f"[belief] {key}: {value}",
                            PRIORITY["belief"]))
        journal = desk_context.get("recent_journal") or []
        n = len(journal)
        for i, entry in enumerate(journal):
            out.append(Item(f"journal:{i}",
                            f"[journal] {entry.get('kind')}: "
                            + str({k: v for k, v in entry.items() if k not in ('ts',)}),
                            PRIORITY["journal"], recency=(i + 1) / n))
    return out
