# The Edge Doctrine — what "cheating" means here, and what we do instead

Asked directly: *what could we do to get an advantage? Think about
cheating.* The answer has a bright line through it, so this file states
the line first and the edges second.

## The line

**We do not:** trade on material non-public information; front-run
anyone's order flow; spoof, layer, or wash trade; post on boards we
trade from (talking our own book is manipulation — `agent/rumors.py`
has no write path *by construction*); scrape private data; track or
profile individuals; or evade the pattern-day-trader rule by splitting
across accounts. These aren't just illegal or against terms — every one
of them is also the fastest way to lose the account and end the project.

**We do:** consume public information faster, more systematically, and
with better memory than a human, and refuse trades a human would take
out of boredom. Everything below is public data, politely fetched,
properly identified.

## The edges, ranked by value per dollar

### 1. The scheduled-event calendar (`agent/events.py`) — free

FOMC 14:00 ET, CPI 08:30, earnings after close — pre-announced to the
minute. `data/events.json` holds the calendar; every cycle the runner
computes active blackout windows and `decide()` refuses **entries**
(exits always work) into them. `flatten: true` events (FOMC) also send
the book flat before the print. The reaction is tradeable; the print is
a coin toss. Seed the template: `python -m agent.events seed`, then keep
the dates current — this is a human chore worth doing weekly.

### 2. Slippage measurement (in `execute()` + `metrics.slippage_stats`)

Every fill records what it paid versus mid. The scoreboard's **"spread
paid"** line is the dollars a marketable limit at mid would have saved —
on a 40-trade day the spread IS the P&L. When that number gets big
enough, it justifies building smarter order routing; until then, it's
the honest cost of urgency, measured instead of guessed.

### 3. Overnight rumor scan + next-day backtrace (`agent/rumors.py`)

The night before each session (21:30 and 05:45 timers), scan public
subreddit listings for watchlist chatter and log the **aggregate**
per-ticker picture: mentions, summed sentiment, sample headlines —
timestamped, so rumors are only ever analyzed against moves that came
AFTER the scan. After each close, `grade` looks up what every rumored
name actually did and writes the verdict; `calibration` turns the
verdicts into the only number that matters: *do loud overnight rumors
predict direction, or just volatility?* The strategist's 09:35 plan
prompt gets the scan **with its track record attached** — a rumor
without a hit rate is noise wearing a suit, and the prompt says so.

Grading is at the crowd level only. No usernames, ever.

### 4. EDGAR 8-K feed (`agent/edgar.py`)

The SEC publishes filings in near-real-time on a public Atom feed.
Material events (8-K) for watched names land in
`data/edgar_filings.jsonl` minutes after filing — public information,
consumed faster than humans read it. Properly identified User-Agent
(set `EDGAR_CONTACT`), 10-minute poll, watchlist-filtered.

### 5. Halt awareness + board velocity (`poller.py`, `decide()`)

A halted name's last print is frozen fiction: `decide()` takes no entry
and no exit on it, and the poller raises a `halted` alert. Board
**mention velocity** (posts last hour vs the hour before) is the honest
signal inside the boards' noise — acceleration predicts volatility, not
direction, and the signal board shows it as `vel`.

### 6. Short interest (`data/short_interest.json`)

FINRA publishes short interest biweekly; brokers show borrow rates.
Human-maintained file: `{"TSLA": {"short_pct_float": 22.5,
"borrow_rate": 8.1, "as_of": "2026-08-01"}}`. Squeeze-prone names
overshoot in both directions — the signal board's `si` column flags
them so the plan can size or avoid accordingly. (Automating this feed
is future work; the file format is the contract.)

## The honest meta-edge

No boredom, no tilt, no revenge trades, no widened stops — and every
day remembered exactly, graded, and mined forever. Most of the edges
above are small; discipline is the one that compounds.
