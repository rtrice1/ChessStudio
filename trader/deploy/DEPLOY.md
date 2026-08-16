# Deploying to the home box (mock phase)

For the always-on Linux machine. This phase runs everything against the
mock — the point is to have the desk *live somewhere*: accumulating days,
journal entries, and gut memory around the clock, before any real API is
involved.

## Install

```bash
git clone <this repo> && cd <repo>/trader
sudo bash deploy/install.sh            # installs to /opt/trader
```

Needs: any Linux with systemd, Python 3.11+, ~1GB free disk. No pip, no
other dependencies. Reinstalling never clobbers accumulated
`desk_state/` — that's the valuable part.

## Windows stopgap (until the Linux drive arrives)

The agent code is pure Python and runs on Windows as-is — what Windows
lacks is systemd, so nothing is *scheduled*; you run the pieces by hand
in terminals. Two good ways:

**Option A — WSL2 (preferred, it IS Linux):** `wsl --install -d Ubuntu`,
then inside it enable systemd (`/etc/wsl.conf` → `[boot]` /
`systemd=true`, then `wsl --shutdown` once) and run `install.sh`
exactly as above. Timers and all. Caveats: Windows sleep suspends the
whole desk (set power settings to stay awake), and WSL's clock is the
Windows clock — keep it on America/New_York or the timers fire at the
wrong hours.

**Option B — native Windows Python:** `pip install -r requirements.txt`
(this pulls `tzdata`, which Windows needs for timezone support), then
run pieces manually:

```powershell
python -m unittest discover -s tests     # should be all green, same as Linux
python -m mockschwab.server --port 8788  # terminal 1: mock market
python -m agent.dashboard                # terminal 2: http://localhost:8899
python -m agent.run_day                  # a simulated day, any time
python -m agent.rumors scan              # by hand, evening + pre-open
python -m agent.rumors grade             # by hand, after the close
python -m agent.run_live --starting-cash 10000   # Monday, 9:30-16:00 ET
```

The desk's entire state is `data/` + `desk_state/` — when the Linux
drive arrives, copy those two directories over and nothing is lost:
the journal, beliefs, gut memory, and every ledger line move with them.

## What runs

| Unit | What | When |
|---|---|---|
| `trader-mock.service` | mock Schwab API on :8788 | always |
| `trader-poller.service` | Tier-1 watcher, snapshots + alerts + news sentiment | every 60s |
| `trader-simday.timer` | one full simulated trading day (gut memory + desk journal grow nightly) | weekdays 21:00 |
| install-time backfill | 500 simulated days seeded into gut memory | once, if empty |

## Looking in on it

```bash
systemctl status trader-mock trader-poller
journalctl -u trader-simday.service -n 50        # last nightly sim day
cat /opt/trader/desk_state/journal.jsonl | tail  # the desk journal
python3 -c "import sys; sys.path.insert(0,'/opt/trader'); \
  from agent.desk import Desk; print(Desk('/opt/trader/desk_state').wake_summary())"
```

## Care and feeding

- **Kill switch:** `touch /opt/trader/data/HALT` stops all trading
  decisions; only a human removes it.
- **Backups:** `desk_state/` and `data/ledger.jsonl` are the project's
  memory — the code is reproducible, they aren't.
  `tar czf desk-$(date +%F).tgz /opt/trader/desk_state` on a cron, copied
  anywhere off-box.
- **Clock:** the simday timer assumes the box's local time; set the
  timezone you actually live in (`timedatectl set-timezone ...`).

## What this phase is for

By the time real credentials enter the picture, the box should already
have months of nightly sim days journaled, a warm gut, and boring, known
operational behavior (restarts, disk, backups). The step to real paper
trading is then: point `BrokerClient` at the real API read-only, switch
the poller to market hours, and add the Tier-2/Tier-3 timers from
SPEC/AGENTS.md — with the LLM tiers calling the Anthropic API using
`agent/focus.py` to build their prompts.
