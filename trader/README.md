# Trader

An agentic paper-trading system, built by Claude on the terms: *"be the
trader, build the software you'd need, spec the agents you'd want, tell me
what you need."* Accepted, with amendments — see [SPEC/NEEDS.md](SPEC/NEEDS.md)
for the renegotiated deal (short version: I can't own money, real trading
happens only behind human-controlled gates, and the risk limits live in
code, not prompts).

## Layout

```
trader/
├── mockschwab/          # Mock Charles Schwab Trader API (stdlib HTTP server)
│   ├── market.py        #   synthetic market: GBM + regime shifts, seedable, time-scalable
│   ├── accounts.py      #   cash account, market/limit orders, fills, P&L
│   └── server.py        #   /v1/marketdata/*, /v1/accounts/* endpoints
├── agent/
│   ├── client.py        # broker HTTP client (mock now, real Schwab later — same interface)
│   ├── poller.py        # Tier 1 (Haiku): scheduled data pull -> snapshots + alerts
│   ├── indicators.py    # SMA/EMA/RSI/ATR over candles
│   ├── risk.py          # HARD limits — every order passes through here, in code
│   ├── strategist.py    # Tier 3: decision engine (rules baseline + LLM decision schema)
│   ├── ledger.py        # append-only JSONL audit trail: fills, rejections, memos
│   └── run_paper.py     # end-to-end accelerated paper session
├── SPEC/
│   ├── AGENTS.md        # the agent fleet: model tiers, schedules, authority, deploy shape
│   ├── STRATEGY.md      # baseline strategy, benchmarks, honest caveats
│   └── NEEDS.md         # what Claude needs from the human, and the deal terms
└── tests/
```

## Run it

Python 3.11+, no dependencies.

```bash
cd trader

# tests
python -m unittest discover -s tests -v

# accelerated paper session: 20 cycles, each ~5 sim-minutes
python -m agent.run_paper --cycles 20 --time-scale 300

# or run the pieces separately:
python -m mockschwab.server --port 8788 &         # mock brokerage
python -m agent.poller --interval-seconds 300     # Tier-1 watcher loop
```

Everything the system does lands in `data/ledger.jsonl` (append-only) and
`data/snapshots/`. To halt all trading: `touch data/HALT`.

## Safety model in one paragraph

Models propose; code disposes. The strategist — LLM or rules — emits
`Decision` objects, and every one passes through `agent/risk.py`: 10% max
per position, no margin/shorting, 15% max order, −2% daily-loss circuit
breaker, file-based kill switch. Those limits change only by a human
editing the file. Going from mock → real-data paper → real money crosses
explicit gates listed in [SPEC/AGENTS.md](SPEC/AGENTS.md), each flipped by
the human, none by an agent.
