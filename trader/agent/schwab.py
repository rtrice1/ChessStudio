"""Schwab Trader API client — real market data, translated to our shapes.

Scope and posture:
- **Data and account visibility only.** `place_order` and `cancel_order`
  raise, unconditionally. There is no env var, flag, or code path in this
  file that sends an order to Schwab. Live order routing, if it ever
  happens, is a separate deliberate build behind the gates in
  SPEC/AGENTS.md — shadow mode (agent/shadow.py) fills orders in a local
  book against these real quotes instead.
- Same method surface and return shapes as BrokerClient/the mock, so the
  poller, strategist, and gut consume real data unchanged.
- stdlib urllib only, matching the rest of the desk.

OAuth mechanics (Schwab): a one-time browser authorization yields a code
(via the redirect URL) exchanged for tokens; access tokens last ~30
minutes and auto-refresh here; refresh tokens last ~7 days and require
re-running `python -m agent.schwab auth` — a weekly human ritual Schwab
imposes, not us. Setup steps: deploy/SCHWAB.md.

Env: SCHWAB_APP_KEY, SCHWAB_APP_SECRET, SCHWAB_REDIRECT_URI (default
https://127.0.0.1), SCHWAB_TOKEN_PATH (default data/schwab_tokens.json).
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.schwabapi.com"
DEFAULT_TOKEN_PATH = os.path.join(os.path.dirname(__file__), "..", "data",
                                  "schwab_tokens.json")


class SchwabError(Exception):
    pass


class OrdersDisabled(SchwabError):
    """Raised by any attempt to route a real order. Not configurable."""


def occ_to_schwab(symbol: str) -> str:
    """Our OCC form AAPL260821C00190000 -> Schwab's padded-root form
    'AAPL  260821C00190000' (root space-padded to 6 chars)."""
    import re
    m = re.match(r"^([A-Z]{1,6})(\d{6}[CP]\d{8})$", symbol)
    if not m:
        return symbol
    return f"{m.group(1):<6}{m.group(2)}"


class TokenStore:
    def __init__(self, path: str = None):
        self.path = path or os.environ.get("SCHWAB_TOKEN_PATH", DEFAULT_TOKEN_PATH)
        self.app_key = os.environ.get("SCHWAB_APP_KEY", "")
        self.app_secret = os.environ.get("SCHWAB_APP_SECRET", "")
        self._tokens: dict = {}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self._tokens = json.load(f)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self._tokens, f, indent=2)

    def _basic_auth(self) -> str:
        raw = f"{self.app_key}:{self.app_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _token_request(self, form: dict) -> dict:
        req = urllib.request.Request(
            f"{API}/v1/oauth/token",
            data=urllib.parse.urlencode(form).encode(),
            headers={"Authorization": self._basic_auth(),
                     "Content-Type": "application/x-www-form-urlencoded"},
            method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            tokens = json.loads(resp.read().decode())
        tokens["expires_at"] = time.time() + float(tokens.get("expires_in", 1800))
        tokens["refresh_obtained_at"] = self._tokens.get(
            "refresh_obtained_at", time.time())
        if "refresh_token" in tokens:
            tokens["refresh_obtained_at"] = time.time()
        self._tokens = tokens
        self._save()
        return tokens

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        return self._token_request({
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri})

    def access_token(self) -> str:
        if not self._tokens:
            raise SchwabError("no tokens — run: python -m agent.schwab auth")
        if time.time() > float(self._tokens.get("expires_at", 0)) - 60:
            self._token_request({
                "grant_type": "refresh_token",
                "refresh_token": self._tokens["refresh_token"]})
        return self._tokens["access_token"]

    def refresh_age_days(self) -> float:
        obtained = float(self._tokens.get("refresh_obtained_at", time.time()))
        return (time.time() - obtained) / 86400.0


class SchwabClient:
    """BrokerClient-shaped, read-only against the real Schwab API."""

    def __init__(self, tokens: TokenStore | None = None, account_id: str = ""):
        self.tokens = tokens or TokenStore()
        self.account_id = account_id  # unused; kept for interface parity
        self._account_hash: str | None = None

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        url = f"{API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.tokens.access_token()}",
            "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    # --- market data, translated to the shapes the desk consumes ---

    def quotes(self, symbols: list[str]) -> dict:
        if not symbols:
            return {}
        wire = [occ_to_schwab(s) for s in symbols]
        raw = self._get("/marketdata/v1/quotes",
                        {"symbols": ",".join(wire), "indicative": "false"})
        out = {}
        for ours, theirs in zip(symbols, wire):
            entry = raw.get(theirs) or raw.get(ours) or {}
            quote = entry.get("quote") or {}
            if not quote:
                out[ours] = {"error": "no quote"}
                continue
            # securityStatus "Halted"/"Closed" means the last print is
            # frozen — decide() refuses to act on halted names either way.
            status = str(quote.get("securityStatus") or "").lower()
            out[ours] = {
                "symbol": ours,
                "bid": float(quote.get("bidPrice") or 0.0),
                "ask": float(quote.get("askPrice") or 0.0),
                "last": float(quote.get("lastPrice") or 0.0),
                "halted": status == "halted",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        return out

    def price_history(self, symbol: str, days: int = 5, interval: int = 5) -> dict:
        raw = self._get("/marketdata/v1/pricehistory", {
            "symbol": symbol, "periodType": "day", "period": days,
            "frequencyType": "minute", "frequency": interval,
            "needExtendedHoursData": "false"})
        candles = []
        for c in raw.get("candles", []):
            candles.append({
                "open": c["open"], "high": c["high"], "low": c["low"],
                "close": c["close"], "volume": c.get("volume", 0),
                "datetime": datetime.fromtimestamp(
                    c["datetime"] / 1000.0, tz=timezone.utc).isoformat(),
            })
        return {"symbol": symbol, "candles": candles}

    def chain(self, symbol: str, expiry: str | None = None) -> dict:
        params = {"symbol": symbol, "contractType": "ALL",
                  "strikeCount": 12, "range": "NTM"}
        if expiry:
            params["fromDate"] = params["toDate"] = expiry
        raw = self._get("/marketdata/v1/chains", params)
        if raw.get("status") == "FAILED":
            return {"error": "chain lookup failed"}

        def translate(exp_map: dict, put_call: str) -> tuple[str | None, list]:
            dates = sorted(exp_map.keys())
            if not dates:
                return None, []
            first = dates[0]                       # nearest expiry
            contracts = []
            for _strike, entries in sorted(exp_map[first].items(),
                                           key=lambda kv: float(kv[0])):
                c = entries[0]
                contracts.append({
                    "contractSymbol": c["symbol"].replace(" ", ""),
                    "strike": float(c["strikePrice"]),
                    "putCall": put_call,
                    "expiry": first.split(":")[0],
                    "bid": float(c.get("bid") or 0.0),
                    "ask": float(c.get("ask") or 0.0),
                    "last": float(c.get("last") or 0.0),
                    "delta": (float(c["delta"])
                              if c.get("delta") not in (None, -999.0) else None),
                    "gamma": c.get("gamma"), "theta": c.get("theta"),
                    "vega": c.get("vega"), "iv": c.get("volatility"),
                })
            return first.split(":")[0], contracts

        call_exp, calls = translate(raw.get("callExpDateMap") or {}, "C")
        put_exp, puts = translate(raw.get("putExpDateMap") or {}, "P")
        return {"symbol": symbol, "expiry": call_exp or put_exp,
                "calls": calls, "puts": puts}

    # --- account (read-only view of the real account) ---

    def _hash(self) -> str:
        if self._account_hash is None:
            numbers = self._get("/trader/v1/accounts/accountNumbers")
            if not numbers:
                raise SchwabError("no accounts visible to this app")
            self._account_hash = numbers[0]["hashValue"]
        return self._account_hash

    def account(self) -> dict:
        raw = self._get(f"/trader/v1/accounts/{self._hash()}",
                        {"fields": "positions"})
        acct = raw.get("securitiesAccount") or {}
        balances = acct.get("currentBalances") or {}
        positions = []
        for p in acct.get("positions", []):
            instrument = p.get("instrument") or {}
            qty = float(p.get("longQuantity", 0)) - float(p.get("shortQuantity", 0))
            positions.append({
                "symbol": str(instrument.get("symbol", "")).replace(" ", ""),
                "quantity": int(qty),
                "averagePrice": float(p.get("averagePrice", 0.0)),
                "marketValue": float(p.get("marketValue", 0.0)),
                "unrealizedPnl": float(p.get("currentDayProfitLoss", 0.0)),
            })
        cash = float(balances.get("cashAvailableForTrading")
                     or balances.get("cashBalance") or 0.0)
        equity = float(balances.get("liquidationValue") or cash)
        return {"accountId": acct.get("accountNumber", "SCHWAB"),
                "cash": cash, "equity": equity, "positions": positions,
                "realizedPnl": 0.0,
                "timestamp": datetime.now(timezone.utc).isoformat()}

    def news(self, symbols: list[str], limit: int = 10) -> dict:
        return {}  # Schwab has no news endpoint; poller tolerates empty

    # --- orders: structurally disabled ---

    def place_order(self, *args, **kwargs):
        raise OrdersDisabled(
            "SchwabClient does not route orders. Shadow mode "
            "(agent/shadow.py) fills locally against real quotes; live "
            "routing is a separate build behind the SPEC/AGENTS.md gates.")

    def cancel_order(self, *args, **kwargs):
        raise OrdersDisabled("SchwabClient does not route orders.")

    def list_orders(self) -> list:
        return []


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["auth", "test"])
    args = ap.parse_args()
    store = TokenStore()

    if args.command == "auth":
        if not store.app_key or not store.app_secret:
            print("Set SCHWAB_APP_KEY and SCHWAB_APP_SECRET first "
                  "(see deploy/SCHWAB.md).")
            return 1
        redirect = os.environ.get("SCHWAB_REDIRECT_URI", "https://127.0.0.1")
        url = (f"{API}/v1/oauth/authorize?"
               + urllib.parse.urlencode({"client_id": store.app_key,
                                         "redirect_uri": redirect}))
        print("1. Open this URL, log in to Schwab, approve the app:")
        print(f"\n   {url}\n")
        print("2. Your browser lands on an error page — that's expected.")
        print("   Copy the FULL address-bar URL and paste it here.")
        pasted = input("redirect URL> ").strip()
        code = urllib.parse.parse_qs(
            urllib.parse.urlparse(pasted).query).get("code", [""])[0]
        if not code:
            print("no ?code= found in that URL")
            return 1
        store.exchange_code(urllib.parse.unquote(code), redirect)
        print(f"tokens saved to {store.path} (mode 600). "
              "Refresh token lasts ~7 days; re-run weekly.")
        return 0

    # test: prove data access end to end, read-only
    client = SchwabClient(store)
    print(f"refresh token age: {store.refresh_age_days():.1f} days "
          "(Schwab expires them at ~7)")
    quotes = client.quotes(["SPY", "AAPL"])
    for sym, quote in quotes.items():
        print(f"{sym}: {quote}")
    acct = client.account()
    print(f"account {acct['accountId']}: equity {acct['equity']:.2f}, "
          f"cash {acct['cash']:.2f}, positions {len(acct['positions'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
