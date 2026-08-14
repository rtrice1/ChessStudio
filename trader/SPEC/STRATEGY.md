# Strategy Notes

## Baseline (implemented, `agent/strategist.py` rules mode)

Deliberately boring mean-reversion with a trend filter, on a 10-name
large-cap watchlist:

- **Entry:** RSI(14) < 32 while price holds above 97% of SMA20 — oversold
  but not in freefall. Size: ~5% of equity per entry.
- **Exit:** −4% stop from average cost, +6% take-profit, or RSI(14) > 68.
- No shorting, no margin, no options, no overnight leverage. Cash account
  semantics only.

This baseline exists to be beaten. It's the benchmark the LLM strategist
(Tier 3) has to outperform — after token costs — to justify existing. The
other benchmark is buy-and-hold SPY, which is the benchmark *everything*
has to beat, and honestly most things don't.

## What Tier 3 ("me") adds when live

The rules can't read. I can: earnings dates, Fed days, sector correlation
("all ten names are down 2% — that's not ten signals, that's one macro
signal"), and the difference between a stock that's oversold and a stock
that's correctly repriced on news. The Tier-3 prompt gets the same
snapshot the rules get, plus recent ledger memos, and must output the same
`Decision` schema. Same risk gate. The experiment is whether judgment on
top of structure beats structure alone.

## Evaluation protocol

- Every session logs to `data/ledger.jsonl`; every decision has a written
  rationale, so wins and losses can be attributed to reasoning, not vibes.
- Weekly: P&L vs. SPY-hold and vs. rules-baseline over the same window.
- A strategy change requires a written note in this file first (what's
  changing, why, what would prove it wrong). No silent drift.

## Known honest problems

- The mock market is GBM with regime shifts — it has no news, no
  earnings, no fat tails beyond what the regimes fake. Success against
  the mock proves the *plumbing*, not the strategy.
- Paper fills are optimistic (no real queue, tiny slippage model).
- 30 days of paper is statistically almost nothing; it screens for
  "obviously broken," not "actually good." Anyone who tells you
  otherwise is selling something.
