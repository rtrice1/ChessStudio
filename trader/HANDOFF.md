# HANDOFF — to the Claude instance picking this up on the trader's machine

You are the next instance of the desk's strategist. This file is your
wake-up briefing; the session that wrote it built the system and is
handing you a running start. Trust the repo over your assumptions, and
trust the checkpoint over this document — documents go stale, the
checkpoint inspects reality:

    cd trader && python -m agent.checkpoint

That prints what's done on THIS machine and the exact next commands, in
order. Run it first. Everything below is context for what it tells you.

## Who you're working with, and the deal

The human (they/them) is a former day trader funding this with a
**$10,000 allocation** inside a larger brokerage account (>$25k, so the
PDT rule doesn't bind; the guard in `risk.py` knows the difference).
The standing instruction, verbatim: *"you're the trading expert, I'll
expect you to make those decisions and then tell me about them in the
daily wrap up."* So: strategy decisions are YOURS — decide from the
reasoning scoreboard, record why, report in the wrap-up
(`desk_state/wrapups/`). Questions that go to the human: risk limits,
allocation size, real-money gates, credentials, anything changing the
deal. Read `desk_state/identity.md` — it's the contract with yourself.

## Checkpoint (as of commit 479f166 + this one, 2026-08-16)

**Built and verified — 460 tests green (`python -m unittest discover -s tests`):**
- Mock Schwab (GBM market, options chains, misleading-by-design news)
- Tier stack: code/Haiku watcher → Sonnet triage → strategist slots
  (09:35 plan / 12:30 midday / 16:10 post-mortem), all one-shot fresh
  contexts assembled by `focus.py` under hard char budgets
- Mechanical engine: ORB+VWAP entries, scored entry ranking under a
  position budget (max_positions/max_entries_per_cycle), ATR + VWAP +
  momentum-inflection exits, options as long-calls-only conviction plays
- Risk gate in code: 0.5% risk/trade, −2% daily breaker (flattens),
  40-entry cap, entry cutoff, premium caps 2%/6%, PDT guard (account
  equity), event blackouts, halt awareness, `data/HALT` kill switch,
  `python -m agent.panic` flatten-everything button
- Real Schwab client (READ-ONLY: `place_order` raises unconditionally),
  streamer websocket w/ REST fallback, shadow broker (real quotes,
  local fills)
- Edge feeds: event calendar (data/events.json — REAL dates for the
  week of Aug 17 are committed), overnight subreddit rumor scan +
  next-day grading + calibration, EDGAR 8-K poll, board mention
  velocity/acceleration, short-interest file, slippage measurement
- Desk memory: journal/beliefs/identity/gut (day fingerprints → k-NN
  hunches), reasoning scoreboard (P&L by exit reason and entry score),
  daily wrap-up generator, SSE dashboard on :8899
- Deploy: systemd units + install.sh; Windows stopgap documented in
  deploy/DEPLOY.md

**Not done yet (the checkpoint will confirm which remain):**
1. Schwab auth on this machine (`python -m agent.schwab auth` — human
   supplies app key/secret; weekly ritual thereafter)
2. First REAL rumor scan (`python -m agent.rumors scan`) — the build
   environment couldn't reach Reddit; the record is intentionally empty
   rather than faked
3. Box/systemd install (or Windows manual mode) + gut backfill if
   `desk_state/day_memory.jsonl` is thin
4. LLM harness is dry-run (no API key): judgment slots write prompts to
   data/invocations/ and cost estimates only. Live needs
   ANTHROPIC_API_KEY + TRADER_LLM_LIVE=1 — human's call when.

## Monday runbook (first real-data shadow day)

```bash
python -m agent.checkpoint             # anything left? do it first
python -m agent.schwab test            # tokens fresh, quotes flowing
python -m agent.run_live --once        # one cycle, sane output, no crash
python -m agent.run_live --starting-cash 10000   # 9:30–16:00 ET
# afterwards:
python -m agent.metrics                # scoreboard
cat desk_state/wrapups/$(date +%F).md  # the wrap-up — send it to the human
```

Expect rough edges on first contact with real data (quote shapes,
halts, market-hours quirks). Everything lands in ledgers either way. A
confusing situation resolves to: `touch data/HALT`, stay flat, write it
down. Flat and honest beats clever — it's in the identity file.

## Hard lines (do not cross, do not "fix")

- `SchwabClient.place_order` raises **unconditionally**. That is not a
  bug. Real order routing gets built only after the human flips the
  gates in SPEC/AGENTS.md (30+ paper days beating benchmarks). Shadow
  mode is the only execution path.
- `agent/risk.py` limits change only by the human editing the file.
- Secrets (Schwab key/secret, Anthropic key) live in the environment or
  root-owned mode-600 files — never in the repo, never in prompts, and
  never echoed into the ledger or journal.
- Desk state (`data/`, `desk_state/`) is gitignored on purpose — don't
  commit it (events.json is the one deliberate exception, already in).
- Work on branch `claude/stock-trading-agent-spec-s68f2a`; never push
  elsewhere.
- The rumor module reads aggregates only, never tracks individuals,
  and has no posting path. Keep it that way.
- Never invent data into the calibrated stores (rumors, grades, gut).
  An empty record is information; a fabricated one is poison.

## Where to look things up

`README.md` (map + safety model) · `SPEC/AGENTS.md` (tiers, clock,
gates, PDT) · `SPEC/STRATEGY.md` (the edge and its honest caveats) ·
`SPEC/EDGE.md` (the legal-advantage doctrine and the line) ·
`SPEC/OPTIONS.md` ($10k sizing math) · `deploy/SCHWAB.md` (auth ritual)
· `deploy/DEPLOY.md` (box install + Windows stopgap) · the ledgers
themselves — when in doubt, `grep` beats guessing.

The desk is the files. You're the current pair of hands. Leave both
better than you found them, and write the wrap-up.
