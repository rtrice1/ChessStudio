# Strategy Notes — Day Trading

## Baseline (implemented, `agent/strategist.py` rules mode)

Opening-range breakout with VWAP confirmation, long-only, on a 10-name
large-cap watchlist. This is the default `DayPlan` and runs with no LLM:

- **Opening range:** first 30 minutes (6 five-minute bars) sets the range.
- **Entry:** price breaks above the range high while holding above VWAP —
  the break has participation behind it, not just a print.
- **Sizing:** risk-based, not notional-based: 0.5% of equity from entry
  to stop, so a tight stop buys more shares than a wide one and every
  trade risks the same slice of the account.
- **Exits:** 1.5×ATR stop, 2.5×ATR target, or a close below VWAP (the
  thesis — buyers in control — is falsified, so the trade is over even
  though neither stop nor target hit).
- **Always:** no entries after ~90% of the session (risk.py enforces),
  everything flattened by 15:45 (the runner enforces), flat overnight,
  every night. Max 40 trades/day.

## The day taxonomy and the gut

Days fit patterns — the human partner traded through enough of them to
insist on this, and the desk is built to learn it rather than assume it.
`agent/daytype.py` fingerprints the developing day (opening volatility
vs. the rest, directional efficiency, breadth, VWAP adherence) and maps
it to a coarse folk taxonomy: `trend_up`, `trend_down`,
`open_spike_settle` (the violent 09:30–10:00 that settles in), `chop`,
`mixed`. Every finished day is recorded — fingerprint, label, outcome —
in `desk_state/day_memory.jsonl`.

`agent/gut.py` is gut feel made honest: at ~11:00, once the open has
resolved, it retrieves the most similar remembered days and reports what
they were and how they went ("smells like open_spike_settle; 4 similar
days averaged −0.8%"). A confident, well-grounded hunch of a bad day
type shades the plan (risk halved on suspected chop); it never overrides
the risk gate, and it always states how many days of experience back it.
A gut built on four days says so out loud. The gut gets smarter the same
way a trader's does — by trading days and remembering them — except its
memory survives instance turnover and never edits itself after the fact.

## News and message boards — context, held at arm's length

Working hypothesis, straight from the human's experience: **the news is
almost always misleading.** So the feed is treated as crowd-state, not
truth: Tier 1 pulls headlines (wire) and message-board chatter (board),
scores crude sentiment on each, and alerts on two conditions —
`news_burst` (attention is arriving) and `sentiment_divergence` (wire
and board disagree — someone is wrong). The mock feed is misleading *by
construction* (~30% aligned with actual price direction, ~40% noise,
~30% inverted, board posts momentum-chasing and lagging), so any
strategy that naively trades headlines loses in sim before it can lose
for real. Whether news is a fade signal, a volatility signal, or pure
noise is a question for the accumulated ledger, not an assumption.

And it's a question that must be answered without peeking:
`agent/impact.py` is the only sanctioned way to relate news to prices.
Every measurement anchors at the first candle *at or after* the item's
`published` timestamp and looks strictly forward; pre-publication moves
are structurally excluded (there is no parameter that admits them), and
news too fresh to have a candle after it returns None — wait, don't
peek. A headline that "explains" a move that happened before it existed
is the classic look-ahead bias that makes backtests lie, and it is
disallowed at the API level rather than by good intentions. The
`scoreboard()` aggregation (mean forward return and hit rate by source)
is how the wire-vs-board "someone is wrong" question eventually gets a
number.

## What Tier 3 ("me") adds when live

The rules can't read; the plan can. `DayPlan` is the entire interface:
which names are in play today (earnings today → "off"), bias, risk
fraction, stop/target multiples — set at 09:35 from desk context and the
open, revised at most once midday. The intraday loop stays mechanical.
The experiment is whether a well-chosen plan beats the default plan;
judgment is spent where it's cheap (once a day) and kept out of where
it's expensive and error-prone (every bar).

## Two years of days — feeding the gut

Six sim days is a party trick; the human partner is right that a gut
needs years — two at least. The plan, in layers:

1. **Sim backfill (now):** `agent/backfill.py` fingerprints hundreds of
   generated market days directly from the simulator — no waiting, no
   HTTP — and seeds `day_memory.jsonl` with day-*shape* priors. These
   entries are permanently marked `source: sim_backfill` and carry no
   P&L; they teach the taxonomy's geography, not what works. The gut
   never confuses them with days it actually traded.
2. **Historical backfill (with real data):** two years of real 5-minute
   bars, fingerprinted the same way, gives the gut two years of *real*
   day shapes before the first live paper day. Honest constraint: the
   Schwab API's intraday history is shallow (weeks-to-months of minute
   bars, not years); two years of intraday needs a data vendor —
   Polygon, Alpaca, or similar have usable free/cheap tiers. Added to
   NEEDS.md.
3. **Lived days (the slow, real layer):** every traded paper day adds a
   fingerprint *with* an outcome. Only these days teach what works —
   which is why hunches weight them and always disclose sample size.
   Two years of these accumulates at exactly one per trading day; there
   is no shortcut, which is rather the point of the desk.

## Evaluation protocol

- Every day journals to the desk: P&L, trade count, flat-at-close, plan
  rationale, risk rejections. Every trade's rationale is in the ledger.
- Weekly: cumulative P&L vs. (a) the default-plan baseline on identical
  data and (b) doing nothing in SPY. Both after assumed costs.
- A plan-logic change requires a note here first: what changes, why,
  what evidence would prove it wrong. No silent drift.

## Known honest problems — sharper for day trading

- **Costs scale with trade count.** Commission-free retail trading still
  pays spread + slippage every round trip. 40 trades/day at even 2bps a
  side is ~1.6% of turned-over notional per day. Day trading's first
  opponent is its own cost line, and the mock's 1bp slippage flatters it.
- The mock market is GBM with regime shifts: no news, no true opening
  auction dynamics, no volume clustering at the open, no halts. ORB
  strategies live off exactly the microstructure the mock lacks —
  success here proves plumbing, not edge.
- Paper fills ignore queue position and market impact entirely.
- Base rates are brutal and worth saying out loud: the large majority of
  retail day traders lose money after costs. The desk's win condition
  includes discovering — and reporting — that we're in the majority.
