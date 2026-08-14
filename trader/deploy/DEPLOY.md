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
