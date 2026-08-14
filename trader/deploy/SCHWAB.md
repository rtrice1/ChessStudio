# Schwab Setup — do steps 1–3 BEFORE Monday

Schwab's developer onboarding has a human approval step that can take
**1–3 business days**. Registering this weekend is what makes a Monday
dry run possible.

## 1. Developer account + app registration (do today)

1. Go to https://developer.schwab.com and create a developer account
   (separate login from your brokerage account).
2. Create an "Individual Developer" app:
   - **API product:** add BOTH "Accounts and Trading Production" and
     "Market Data Production".
   - **Callback URL:** `https://127.0.0.1` (exactly; we use the manual
     paste flow, no server needed).
   - **App name/description:** anything ("desk", "personal trading bot").
3. Submit and wait for status **"Ready For Use"** — this is the 1–3 day
   human step. "Approved - Pending" is not ready yet.
4. When ready, note the **App Key** and **App Secret** from the app page.

## 2. Put credentials on the box (or laptop for the first run)

```bash
export SCHWAB_APP_KEY="your-app-key"
export SCHWAB_APP_SECRET="your-app-secret"
# on the box, persist them in /etc/trader/llm.env style: root-owned, mode 600
```

## 3. One-time (well, weekly) authorization

```bash
cd trader && python -m agent.schwab auth
```

Opens nothing itself — it prints a URL. Open it in a browser, log in
with your **brokerage** credentials, approve the app for your account.
The browser then lands on an unreachable `https://127.0.0.1/?code=...`
page — that's expected; copy the full address-bar URL and paste it back
into the prompt. Tokens are saved to `data/schwab_tokens.json` (mode
600). **Schwab expires refresh tokens after ~7 days** — this step is a
weekly ritual, by their design. `python -m agent.schwab test` verifies
data access and prints the token's age.

## 4. Monday's dry run

```bash
python -m agent.schwab test          # quotes + account visibility
python -m agent.run_live --once      # one full poll/decide cycle
python -m agent.run_live             # the real thing, 9:30-16:00 ET
python -m agent.metrics              # scoreboard afterwards
```

`run_live` is **shadow mode and only shadow mode**: real Schwab quotes,
chains, and account visibility; every order fills in a local book
(`data/shadow_book.json`). `SchwabClient.place_order` raises
unconditionally — there is no flag that arms real orders. Options
entries (`--instrument calls`) shadow-fill against real chain quotes,
which is where we find out what real spreads do to the strategy.

## What Monday tells us

First contact between the whole desk — poller, gut, focus, risk gate,
strategist plan, scoreboard — and a real market day. Expect rough edges:
real quote shapes we haven't seen, market-hours quirks, symbols halted.
Everything lands in the ledgers either way, and the post-mortem writes
itself into the desk journal like any other day.
