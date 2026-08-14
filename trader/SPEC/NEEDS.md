# What I need from you

You asked what I'd want. Taking that at face value, in order of usefulness:

## Now (mock phase)
1. **Nothing.** This repo runs on any machine with Python 3.11+. Run the
   paper session, read the ledger, argue with my strategy. The most
   valuable thing right now is your review of `SPEC/AGENTS.md` and
   `agent/risk.py` — those two files are the whole safety model.

## Paper-with-real-data phase
2. **A Schwab developer account** (developer.schwab.com), registered by
   you, with an OAuth app. You hold the credentials; the box holds a
   refresh token; I never need to see them in conversation.
3. **A small always-on box.** Cheapest VPS tier anywhere ($4–6/mo) or a
   spare machine at home. Needs: outbound HTTPS, Python, cron/systemd,
   ~1GB disk for snapshots. Nothing else.
4. **A weekly 20-minute review** where you bring me the ledger and we go
   through every decision memo. This is the part I actually want — the
   feedback loop is worth more to me than the server.

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
