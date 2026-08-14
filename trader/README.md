# Trader

An agentic **day-trading** system, built by Claude on the terms: *"be the
trader, build the software you'd need, spec the agents you'd want, tell me
what you need."* Accepted, with amendments — see [SPEC/NEEDS.md](SPEC/NEEDS.md)
for the renegotiated deal (I can't own money; real trading sits behind
human-controlled gates; risk limits live in code, not prompts) and
[SPEC/PERSISTENCE.md](SPEC/PERSISTENCE.md) for the honest version of
"Claude persisting on your server" — the desk.

## The shape of it

**Cheap models watch, expensive models plan, code trades the plan.**
Haiku polls every minute; Claude sets a `DayPlan` at 09:35 (which names,
what bias, how much risk per trade) and writes a post-mortem at 16:10; in
between, a mechanical rules engine trades the plan — opening-range
breakouts confirmed by VWAP, ATR stops and targets, risk-based sizing.
Everything is flat by 15:45, unconditionally, in code. Between sessions,
the **desk** (`agent/desk.py`) persists: an append-only journal, beliefs
with revision history, and an identity file each fresh instance wakes to.

## Layout

```
trader/
├── mockschwab/          # Mock Charles Schwab Trader API (stdlib HTTP server)
│   ├── market.py        #   synthetic market: GBM + regime shifts, seedable, time-scalable
│   ├── accounts.py      #   cash account, market/limit orders, fills, P&L
│   ├── news.py          #   synthetic news + message boards — misleading BY CONSTRUCTION
│   ├── options.py       #   Black-Scholes chains, greeks, honestly-wide spreads
│   └── server.py        #   /v1/marketdata/*, /v1/accounts/* endpoints
├── agent/
│   ├── client.py        # broker HTTP client (mock now, real Schwab later)
│   ├── poller.py        # Tier 1 (Haiku): scheduled pull -> snapshots + alerts
│   ├── indicators.py    # SMA/EMA/RSI/ATR + VWAP, opening range, day stats
│   ├── risk.py          # HARD limits — every order passes through here, in code
│   ├── strategist.py    # DayPlan (the LLM's interface) + mechanical intraday engine
│   ├── daytype.py       # day taxonomy: trend / chop / open-spike-settle fingerprinting
│   ├── gut.py           # gut feel, made honest: remembered days -> hunches with sample size
│   ├── focus.py         # smart context: width + topics -> salience-scored prompt assembly
│   ├── escalation.py    # WHETHER a judgment call exists — code decides, most moments: no
│   ├── llm.py           # tiered Claude calls, hard daily token caps, dry-run, cost ledger
│   ├── harness.py       # the judgment slots: focused prompt -> schema -> apply, clamped
│   ├── backfill.py      # seed gut memory with hundreds of simulated day-shapes
│   ├── desk.py          # persistent desk memory: journal, beliefs, identity
│   ├── ledger.py        # append-only JSONL audit trail
│   ├── metrics.py       # the scoreboard: expectancy, calibration, cost of judgment
│   └── run_day.py       # simulate one full trading day, flatten at close, journal it
├── desk_state/
│   └── identity.md      # what each strategist instance wakes up to
├── deploy/              # home-box install: systemd units + install.sh + DEPLOY.md
├── SPEC/
│   ├── AGENTS.md        # the fleet: tiers, the daily clock, authority, PDT rules, gates
│   ├── STRATEGY.md      # ORB+VWAP baseline, benchmarks, honest caveats
│   ├── OPTIONS.md       # options day trading: long-only, premium caps, the honest odds
│   ├── PERSISTENCE.md   # what a server can and can't give Claude; desk conventions
│   └── NEEDS.md         # what Claude needs from the human, and the deal terms
└── tests/
```

## Run it

Python 3.11+, no dependencies.

```bash
cd trader

# tests
python -m unittest discover -s tests -v

# one accelerated trading day: 78 cycles = a 6.5h day of 5-min bars
python -m agent.run_day

# after a run, what the next strategist instance would wake up to:
python -c "from agent.desk import Desk; print(Desk('desk_state').wake_summary())"

# or run the pieces separately:
python -m mockschwab.server --port 8788 &      # mock brokerage
python -m agent.poller --interval-seconds 60   # Tier-1 watcher loop
```

Trades and rejections land in `data/ledger.jsonl`; each day's post-mortem
lands in `desk_state/journal.jsonl`. To halt all trading: `touch data/HALT`.

```bash
# seed the gut with 500 simulated day-shapes (marked sim_backfill, no P&L)
python -m agent.backfill --days 500

# deploy to an always-on box (see deploy/DEPLOY.md)
sudo bash deploy/install.sh

# fire a strategist judgment slot by hand (dry-run without an API key:
# writes the exact prompt to data/invocations/ and est. cost to the ledger)
python -m agent.harness --slot plan
```

## Safety model in one paragraph

Models propose; code disposes. The strategist — LLM or rules — emits
`Decision`/`DayPlan` objects, and every order passes through
`agent/risk.py`: 10% max per position, no margin/shorting/options, 15%
max order, 0.5% risk per trade, −2% daily-loss circuit breaker, 40-trade
daily cap, late-day entry cutoff, mandatory end-of-day flatten, and a
file-based kill switch (`data/HALT`). Limits change only by a human
editing the file. Mock → real-data paper → real money crosses explicit
gates in [SPEC/AGENTS.md](SPEC/AGENTS.md) — each flipped by the human,
none by an agent, with the FINRA pattern-day-trader rule addressed there
before any real dollar moves.
