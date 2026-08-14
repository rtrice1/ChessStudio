# Agent Fleet Specification — Day Trading

How I want the local agents to run on the always-on box. Organizing
principles: **cheap models watch, expensive models plan, code trades the
plan, and code — not any model — enforces the limits.** Day trading makes
the second principle sharper: no LLM belongs in a 1-minute decision loop
(too slow, too expensive, too tempted to narrate), so the LLM's job is
compressed into a few planning moments and the intraday loop is mechanical.

## The day, on the clock (ET)

| Time | Who | What |
|---|---|---|
| 08:45 | Tier 1 (Haiku) | Pre-market pull: gaps, prior-day context, halts |
| 09:35 | **Tier 3 (Fable)** | **Sets the DayPlan** from desk context + opening snapshots |
| 09:30–16:00 every 1 min | Tier 1 (Haiku) | Poll quotes/candles + news/boards, indicators (VWAP, opening range, ATR), sentiment, alerts |
| continuous | Rules engine (code) | Executes the DayPlan: ORB entries, ATR stops/targets, VWAP exits |
| ~11:00 | Gut check (code) | Fingerprint the developing day, retrieve similar remembered days, shade the plan (never the gate) |
| 12:30 | Tier 3 (Fable) | Optional single plan revision (or Tier 2 wake) |
| on alerts | Tier 2 (Sonnet) | Triage: cancel stale orders, wake Tier 3, or ignore |
| ~15:05 | risk.py | Entry cutoff — exits only from here |
| 15:45 | Runner (code) | **Unconditional flatten. The day ends in cash, always.** |
| 16:10 | Tier 3 (Fable) | Post-mortem: classify the day's type, commit it to gut memory, journal the day — every trade, what worked, what to change |

## Tiers and authority

### Tier 1 — Watcher (Haiku)
`agent/poller.py`. 1-minute cadence during the session. Read-only; no
order authority. Computes the intraday state the rules engine consumes:
VWAP, opening range, ATR, RSI, alerts — plus the context feed: wire
headlines and message-board chatter with crude sentiment scoring, and
the `news_burst` / `sentiment_divergence` alerts. On real deployment
the feed sources are RSS/newswire APIs and board scrapes (respecting
each site's terms); the mock generates a deliberately misleading feed
so nothing learns to trust headlines. ~95% of API calls, near-zero cost.

### Tier 2 — Triage (Sonnet)
Wakes on alerts (big moves, RSI extremes, broken data). May cancel stale
WORKING orders. May wake Tier 3 with a one-paragraph memo. May NOT open
positions or touch the plan itself.

### Tier 3 — Strategist (Fable — "me")
Runs at 09:35, optionally 12:30, and 16:10. Reads `desk_state/` (see
PERSISTENCE.md) and `data/latest.json`; emits a `DayPlan`
(`agent/strategist.py`): which symbols are in play, per-symbol bias,
per-trade risk fraction, stop/target ATR multiples. The intraday rules
engine executes that plan mechanically. Tier 3 never places orders
directly mid-session; it changes the plan, and only at its scheduled
moments. The 16:10 post-mortem classifies the finished day
(`agent/daytype.py`), commits fingerprint + outcome to gut memory
(`agent/gut.py`, `desk_state/day_memory.jsonl`), and writes the desk
journal entry the next morning's instance will wake up to. Plans should
cite the gut's hunch when they lean on it — and cite its sample size,
because a gut built on four days is a guess wearing a trench coat.

## Focus — how context reaches Tier 3

No tier ever receives "the whole context chain of everything."
`agent/focus.py` builds each Tier-3 prompt as a *focus*: a subsystem
assesses the situation (positions, drawdown, alerts, hunch, time of day)
and sets a width — wide open and reflective on a quiet flat morning,
locked onto two symbols when alerts fire on held positions — then
assembles only what clears a salience bar into a hard budget, and lists
what it deliberately excluded. Salience = source priority (risk state
always; news last — it's misleading by construction) × topic match
(amplified when narrow, crushed when narrow and off-topic) × recency.
Assembly is self-interacting: if what got selected implies different
topics than assumed, focus re-forms around them and reassembles — focus
defined by the interaction it's having, not prescribed in advance. The
heuristic scorer is deliberately swappable for a Haiku call (the "focus
agent") on the box: same interface, smarter salience, still cheap. The
excluded-list goes in the ledger with the decision, so a post-mortem can
ask not just "what did I know" but "what had I chosen not to look at."

## Hard limits (enforced in `agent/risk.py`, not in prompts)

| Limit | Value |
|---|---|
| Max position per symbol | 10% of equity |
| Max gross exposure | 100% — cash account, no margin, no shorting, no options |
| Max single-order notional | 15% of equity |
| Per-trade risk (plan default) | 0.5% of equity, entry-to-stop |
| Daily loss circuit breaker | −2% from day open → flatten everything, done for the day |
| Max trades per day | 40 |
| Entry cutoff | no new entries after ~90% of the session |
| End of day | flat by 15:45, unconditionally, in code |
| Kill switch | `data/HALT` file halts everything; only a human deletes it |

No agent tier may modify `risk.py`, ledger history, the desk journal
(append-only), or the kill switch.

## Regulatory reality check (human decisions, before real money)

- **PDT rule (FINRA):** a margin account making 4+ day trades in 5
  business days must hold ≥ $25,000 equity. Under that, the account gets
  frozen for 90 days. So real day trading means either ≥$25k in a margin
  account or a cash account instead.
- **Cash account day trading** avoids PDT but proceeds settle T+1 —
  trading with unsettled funds triggers good-faith violations. The
  strategist must then treat settled cash, not cash, as the budget.
- Wash-sale rules make the tax accounting of frequent same-symbol trades
  genuinely annoying; the ledger records everything needed, but a human
  (or their accountant) owns filing.

## Deployment shape

One small always-on Linux box. Python 3.11+, stdlib only. Processes under
systemd: `poller.service` (Tier 1 loop), `rules-engine.service`
(intraday executor), `strategist.timer` (09:35 / 12:30 / 16:10 Tier-3
invocations), `triage.path` (alert-driven Tier 2). All state on disk as
JSON/JSONL under `data/` and `desk_state/`. The ledger and desk journal
are append-only audit trails. Tier 1 runs with read-only API credentials
where the brokerage supports scoping.

## Path to real money — gates, in order

1. **Mock** (now): accelerated sim days until the plumbing is boring.
2. **Paper, real data:** real Schwab API, paper account, ≥30 trading
   days. Human reviews the ledger + desk journal weekly with me.
3. **Real, small:** only if (a) paper beats both the no-LLM rules
   baseline and sitting in SPY over the window, (b) the human has read
   the worst day's ledger, (c) the PDT/cash-account choice above is made
   deliberately, and (d) the account is money the human can lose
   outright. Human sets up credentials; I never see or store long-lived
   keys.
4. Default at every gate is "don't advance." Only the data advances a
   gate; boredom doesn't.
