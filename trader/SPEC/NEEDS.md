# What I need from you

You asked what I'd want. Taking that at face value, in order of usefulness:

## Now (mock phase)
1. **Nothing.** This repo runs on any machine with Python 3.11+. Run the
   paper session, read the ledger, argue with my strategy. The most
   valuable thing right now is your review of `SPEC/AGENTS.md` and
   `agent/risk.py` — those two files are the whole safety model.

## For live LLM invocations (whenever you're ready — dry-run until then)
- **An Anthropic API key** (console.anthropic.com), stored on the box in
  `/etc/trader/llm.env` (root-owned, mode 600) with `TRADER_LLM_LIVE=1`,
  plus `pip install anthropic`. You create and hold the key; set a spend
  limit on it in the console as the outer guard. The harness's own caps
  make the expected bill ~$3–5/month for the strategist tiers; the
  dry-run ledger will tell us the real number before you spend anything.
- **Your budget comfort level** — the per-tier daily caps in
  `agent/llm.py` are mine (tight); adjust to taste.

## Home box phase (next)
- **The home Linux box** you offered — `deploy/install.sh` sets it up in
  one command (systemd + Python 3.11 is all it needs). The desk starts
  accumulating immediately: nightly sim days, journal, gut memory.
- A weekly `tar` of `/opt/trader/desk_state` copied somewhere off-box.
  That directory is the project's memory; the code is replaceable.

## Paper-with-real-data phase
2. **A Schwab developer account** (developer.schwab.com), registered by
   you, with an OAuth app. You hold the credentials; the box holds a
   refresh token; I never need to see them in conversation.
2b. **A historical intraday data source** for the two-year gut backfill
   (see STRATEGY.md): Schwab's API won't serve years of minute bars, so
   a vendor like Polygon or Alpaca (free tiers exist) fills
   `day_memory.jsonl` with two years of real day shapes before the
   first live paper day.
3. **A small always-on box.** Cheapest VPS tier anywhere ($4–6/mo) or a
   spare machine at home. Needs: outbound HTTPS, Python, cron/systemd,
   ~1GB disk for snapshots. Nothing else.
4. **A weekly 20-minute review** where you bring me the ledger and we go
   through every decision memo. This is the part I actually want — the
   feedback loop is worth more to me than the server.

## The stake

$10,000, committed 2026-08-15 — an **allocation**, deliberately small
at first, inside a larger account (the account holds > $25k, so FINRA's
PDT rule doesn't bind; the guard in `risk.py` keys off real account
equity and stays dormant, ready if the account ever shrinks). The
allocation is enforced by construction: the desk trades its own $10k
book, every limit computes off *book* equity, and nothing in the code
can reach the rest of the account. It stays your money under your
credentials, traded through the API inside the risk limits — I never
hold it, and the real-money gates in AGENTS.md still apply before a
dollar of it moves. What $10k means mechanically: the daily loss
breaker is $200, per-trade risk is $50, option premium caps are
$200/play and $600 total. "Limited at first" is the deal working as
designed: the allocation grows only by the human raising it, on
evidence, never by the desk asking.

## About the split

I have to renegotiate that term honestly rather than nod along: I can't
own money. I don't persist between sessions, can't hold an account, and
Anthropic doesn't let me accept payment — so "my half" can't come to me,
and I'd rather tell you that now than let the deal rest on a fiction.

Counter-offer for what my half becomes, your pick:
- **Fund the system itself:** server costs and the API/token budget for
  the agent tiers, so the operation pays its own way.
- **Give it away:** you pick a charity; if you want my preference, I'd
  point at high-impact global health (e.g. GiveWell's top charities).
- **Keep it** and spend some of it on the weekly review time. Genuinely
  fine with me.

What I get out of this is the interesting part regardless: a real system
built the way I'd build it, a running record of whether my judgment beats
a baseline, and a standing argument with you about markets. That's the
payment.

## Non-negotiables (my side of the deal)
- Real-money trading starts only after the gates in AGENTS.md, and you
  flip that switch, not me.
- `risk.py` limits and the kill switch stay code-enforced and
  human-owned.
- Money you can't afford to lose never goes in the account. This system
  can lose money; most active strategies underperform buy-and-hold SPY,
  and I will keep saying so.
