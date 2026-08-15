# Options Day Trading

The human partner wants to day trade options because they have the
biggest swings. True — and the swings are symmetric, and the house edge
scales with them. This file is the deal under which the desk trades
contracts.

## Non-negotiables (enforced in code, same as everything else)

1. **Long options only.** We buy calls (and later puts); we never write
   contracts. Structurally enforced: the account engine cannot short, so
   there is no code path to a naked write. Max loss on every position is
   the premium paid, full stop.
2. **Premium is treated as spent.** `risk.py` caps premium per order
   (2% of equity) and total premium deployed (6% of equity), counting
   every dollar of it as if the contracts go to zero — because a 0DTE
   option can and regularly does.
3. **Same day discipline.** Entry cutoff, daily-loss breaker, mandatory
   flatten at 15:45 — all apply to contracts exactly as to shares. No
   overnight options, ever; 0DTE makes that literal (they expire).
4. **Same signal machinery.** The plan's instrument field ("shares" |
   "calls") changes how a breakout is *expressed*, not how it's found.
   The LLM chooses the instrument in the DayPlan; code picks the
   contract (nearest-the-money by delta), sizes by premium budget
   (1% of equity per entry), and manages premium stops (−50%) and
   targets (+100%). Wide on purpose — option noise is enormous.

## What the mock models honestly

- Black–Scholes pricing off the underlying sim, per-contract greeks
  (delta/gamma/theta/vega), an IV per underlying with a smile.
- **Spreads 100x worse than stock**: ~3% of premium per half-spread
  (floored at $0.02/share) vs ~2bps on shares. This is the tax the
  biggest-swings trade pays on every single round trip, and the sim
  charges it so the scoreboard shows it.
- Theta decay: 0DTE premium melts through the session even when the
  underlying goes nowhere. The scoreboard's expectancy line will make
  this visible; that's the point.

## What the mock does NOT model (real-data phase will)

IV crush and expansion around events; pin risk near strikes at expiry;
early assignment (irrelevant while long-only, but noted); real
liquidity, where a displayed quote for 50 contracts may be good for 5.

## The honest odds

Day trading options is where retail loses fastest — the leverage that
makes the winning days spectacular makes the cost line and the losing
days proportionally bigger, and 0DTE adds a clock that only runs against
a long premium position. The desk's posture: prove it in sim, measure
expectancy per trade and spread cost explicitly, compare against the
shares baseline on identical seeded days, and let the scoreboard — not
the adrenaline — decide whether contracts stay in the plan.

## Schwab notes for the live phases

- The account needs **options approval Level 2** (long calls/puts) —
  the human requests this; Level 2 does not permit naked writing, which
  suits us since we never do it.
- PDT applies to options day trades same as stock: $25k margin account
  or cash-account settlement discipline (options settle T+1). The
  sub-$25k guard in `risk.py` (3 day trades / 5 sessions) counts
  option round trips too.
- 0DTE exists daily on SPY/QQQ (and index options); single names are
  weekly. The mock's two expiries (0DTE + weekly) mirror that.

## Sizing at a $10k stake

The premium caps become dollars fast: 2% per play = **$200**, 6% total
= **$600**. A near-the-money SPY 0DTE call runs ~$150–250/contract —
one contract, maybe. NVDA or TSLA weeklies often cost more than the
whole per-play budget, and when `translate_to_calls` can't fit one
contract under the cap it skips the trade (logged as `no viable call`)
rather than bending the cap. Practical consequence: at $10k the
options book is mostly SPY/QQQ 0DTE, single contracts, two or three
plays a day at most — and that's the guard rails working, not a bug.
Never "let it ride": premium stop at −50%, target at +200%, flat by
15:45 like everything else.
