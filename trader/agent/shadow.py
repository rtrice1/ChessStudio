"""Shadow broker for paper trading with local order fills."""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mockschwab.accounts import AccountEngine


class SymbolList:
    """Symbol list that accepts both regular symbols and OCC-style options."""

    OCC_PATTERN = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")

    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols

    def __contains__(self, symbol: str) -> bool:
        """Check if symbol is in list or matches OCC pattern."""
        return symbol in self.symbols or self.OCC_PATTERN.match(symbol) is not None

    def __iter__(self):
        """Iterate over base symbols."""
        return iter(self.symbols)

    def __repr__(self):
        return f"SymbolList({self.symbols})"


class DataFeed:
    """Adapter making a data client look like a market for AccountEngine."""

    def __init__(self, data_client, symbols: list[str]) -> None:
        """
        Initialize data feed.

        Args:
            data_client: Object with .quotes(list)->dict, .price_history(...)->dict, .chain(...)->dict
            symbols: List of tradable ticker symbols (watchlist).
        """
        self.data_client = data_client
        self.symbols = SymbolList(symbols)
        self._quote_cache: dict[str, tuple[dict, float]] = {}  # {symbol: (quote, timestamp)}

    def quote(self, symbol: str) -> dict:
        """
        Get quote for a symbol, with 2-second cache.

        Returns:
            {"bid", "ask", "last", ...} or {"error": "<reason>"}
        """
        now = time.monotonic()

        # Check cache
        if symbol in self._quote_cache:
            cached_quote, cached_time = self._quote_cache[symbol]
            if now - cached_time < 2.0:
                return cached_quote

        # Validate symbol (SymbolList handles both regular and OCC symbols)
        if symbol not in self.symbols:
            result = {"error": f"Unknown symbol: {symbol}"}
            self._quote_cache[symbol] = (result, now)
            return result

        # Fetch from data client
        try:
            quotes_dict = self.data_client.quotes([symbol])
            if not quotes_dict or symbol not in quotes_dict:
                result = {"error": f"No quote for {symbol}"}
                self._quote_cache[symbol] = (result, now)
                return result

            inner_quote = quotes_dict[symbol]
            if isinstance(inner_quote, dict) and "error" in inner_quote:
                self._quote_cache[symbol] = (inner_quote, now)
                return inner_quote

            # Extract required fields
            if "bid" not in inner_quote or "ask" not in inner_quote or "last" not in inner_quote:
                result = {"error": f"Missing bid/ask/last for {symbol}"}
                self._quote_cache[symbol] = (result, now)
                return result

            # Cache and return
            self._quote_cache[symbol] = (inner_quote, now)
            return inner_quote

        except Exception as e:
            result = {"error": f"Exception fetching quote: {str(e)}"}
            self._quote_cache[symbol] = (result, now)
            return result


class ShadowBroker:
    """Local paper-trading broker that fills orders from real market data."""

    def __init__(
        self,
        data_client,
        symbols: list[str],
        starting_cash: float = 100_000.0,
        book_path: Optional[str] = None,
    ) -> None:
        """
        Initialize shadow broker.

        Args:
            data_client: Data source (must have .quotes, .price_history, .chain).
            symbols: Tradable symbols.
            starting_cash: Initial cash balance.
            book_path: Optional path to load/save account state.
        """
        self.data_client = data_client
        self.symbols = symbols
        self.book_path = book_path

        # Create market adapter
        self.feed = DataFeed(data_client, symbols)

        # Create account engine
        cash = starting_cash
        positions: dict[str, dict] = {}
        if book_path and Path(book_path).exists():
            cash, realized_pnl, positions = self._load_book(book_path)
        else:
            realized_pnl = 0.0

        self.engine = AccountEngine(market=self.feed, starting_cash=cash)
        self.engine.realized_pnl = realized_pnl
        # Restore open positions too — a book saved mid-day (e.g. after a
        # --once smoke cycle) is not flat, and forgetting the positions
        # makes their cost vanish from equity AND lets the engine re-enter
        # names it already holds (both happened on 2026-08-18).
        self.engine.positions = {
            sym: {"quantity": int(p.get("quantity", 0)),
                  "averagePrice": float(p.get("averagePrice", 0.0))}
            for sym, p in positions.items() if int(p.get("quantity", 0))
        }

    def _load_book(self, path: str) -> tuple[float, float, dict]:
        """Load persisted cash, realized_pnl, and open positions."""
        try:
            with open(path, "r") as f:
                data = json.load(f)
            return (data.get("cash", 100_000.0),
                    data.get("realized_pnl", 0.0),
                    data.get("positions") or {})
        except Exception:
            return 100_000.0, 0.0, {}

    def quotes(self, symbols: list[str]) -> dict:
        """Get quotes for multiple symbols (delegates to data client)."""
        return self.data_client.quotes(symbols)

    def price_history(self, symbol: str, days: int = 5, interval: int = 5) -> dict:
        """Get price history (delegates to data client)."""
        return self.data_client.price_history(symbol, days, interval)

    def chain(self, symbol: str, expiry: Optional[str] = None) -> dict:
        """Get options chain (delegates to data client)."""
        return self.data_client.chain(symbol, expiry) if expiry else self.data_client.chain(symbol)

    def news(self, symbols: list[str], limit: int = 10) -> dict:
        """No news source in shadow broker; return empty."""
        return {}

    def account(self) -> dict:
        """Get account snapshot after processing open orders."""
        self.engine.process_open_orders()
        return self.engine.snapshot()

    def place_order(
        self,
        symbol: str,
        instruction: str,
        quantity: int,
        order_type: str = "MARKET",
        price: Optional[float] = None,
    ) -> dict:
        """Place an order (filled locally, never sent to real API)."""
        return self.engine.place_order(symbol, instruction, quantity, order_type, price)

    def list_orders(self) -> list:
        """List all orders."""
        return self.engine.list_orders()

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an order."""
        return self.engine.cancel_order(order_id)

    def save(self, path: Optional[str] = None) -> None:
        """
        Persist account state to file.

        Saves cash and realized_pnl. If positions are non-empty at save, includes
        them and sets "flat": false to indicate anomaly.
        """
        save_path = path or self.book_path
        if not save_path:
            return

        data = {
            "cash": round(self.engine.cash, 2),
            "realized_pnl": round(self.engine.realized_pnl, 2),
            "date": datetime.now(timezone.utc).isoformat(),
        }

        if self.engine.positions:
            data["positions"] = self.engine.positions
            data["flat"] = False

        with open(save_path, "w") as f:
            json.dump(data, f, indent=2)
