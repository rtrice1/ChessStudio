# Agent Fleet Specification

How I want the local agents to run once this moves off the mock and onto an
always-on box. The organizing principle: **cheap models watch, expensive
models decide, and code — not any model — enforces the limits.**

## Tiers

### Tier 1 — Watcher (Haiku)
- **What:** `agent/poller.py`. Pulls quotes and 5-minute candles for the
  watchlist, computes indicators, writes `data/snapshots/*.json` and
  `data/latest.json`, emits alerts (RSI extremes, >3% moves, SMA20 crosses).
- **Schedule:** every 5 minutes during market hours (09:30–16:00 ET,
  weekdays), one pull at 08:45 ET for pre-market context.
- **Authority:** none. Read-only against the brokerage API. Cannot place,
  modify, or cancel orders. This tier is ~95% of all API calls and should
  cost close to nothing.
- **Haiku's role:** beyond the mechanical pull, a Haiku pass over each
  snapshot writes a 2–3 sentence "anything notable?" annotation. It flags;
  it never concludes.

### Tier 2 — Triage (Sonnet)
- **What:** wakes when Tier 1 emits alerts, or every 30 minutes as a
  heartbeat. Reads the last few snapshots plus open orders. Decides one
  thing: is this worth waking Tier 3?
- **Authority:** may cancel stale WORKING limit orders (>2h old). May NOT
  open positions or increase exposure.
- **Output:** a one-paragraph triage memo appended to the ledger, and
  optionally a wake signal for Tier 3.

### Tier 3 — Strategist (Fable/Opus — this is "me")
- **What:** reads `data/latest.json` (the format is designed to fit in one
  prompt), recent ledger entries, and current positions; produces decisions
  in the `Decision` schema of `agent/strategist.py`: symbol, action,
  quantity, and a written rationale. Every decision — including HOLD — gets
  a memo in the ledger, because the review conversation with the human is
  the actual product of the first month.
- **Schedule:** 10:00 ET, 13:00 ET, 15:30 ET, plus Tier-2 wakes. Rare and
  deliberate on purpose: I don't believe an LLM should be making
  5-minute-bar decisions, and the token cost would eat the returns anyway.
- **Authority:** may propose any order, but every order passes through
  `agent/risk.py` in code. A rejected order is logged and dropped, not
  retried harder.

## Hard limits (enforced in `agent/risk.py`, not in prompts)

| Limit | Value |
|---|---|
| Max position per symbol | 10% of equity |
| Max gross exposure | 100% (cash account, no margin, no shorting, no options) |
| Max single-order notional | 15% of equity |
| Daily loss circuit breaker | −2% from day-open equity halts all buying |
| Kill switch | `data/HALT` file existing halts everything; only a human deletes it |

Changing any of these is a human edit to `risk.py`, reviewed in a PR. No
agent tier may modify `risk.py`, the ledger history, or the kill switch.

## Deployment shape

- One small always-on Linux box (cheapest VPS tier is fine; this is
  I/O-bound, not compute-bound). Python 3.11+, stdlib only — no dependency
  chain to maintain.
- Processes under systemd (or cron for Tier 1): `poller.service` (Tier 1),
  `triage.timer` (Tier 2), `strategist.timer` (Tier 3). Each tier is a
  separate process with separate credentials scope where the brokerage
  supports it (read-only token for Tier 1).
- All state on disk as JSON/JSONL under `data/` — human-greppable, no
  database until there's a reason.
- The ledger (`data/ledger.jsonl`) is append-only and is the audit trail:
  every fill, every risk rejection with its reason, every decision memo.

## Path to real money — gates, in order

1. **Mock** (this repo, now): everything runs against `mockschwab`,
   accelerated clock, until the plumbing is boring.
2. **Paper, real data:** swap `BrokerClient.base_url` to the real Schwab
   API in read-only + paper mode. Run ≥30 trading days. Human reviews the
   ledger weekly with me.
3. **Real, small:** only if (a) paper beats holding SPY over the window or
   we understand exactly why not, (b) the human has read the risk file and
   the worst week's ledger, and (c) the account is one the human can afford
   to lose outright. Human executes the credential setup; I never see or
   store long-lived keys — the box holds them, scoped and revocable.
4. At every gate the default is "don't advance." Boredom is not a reason
   to advance a gate; only the data is.
