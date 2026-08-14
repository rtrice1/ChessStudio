"""Account and order engine for paper trading."""

import uuid
from datetime import datetime, timezone
from typing import Optional

from .market import MarketSim


class AccountEngine:
    """Manages account state, positions, and orders."""

    def __init__(
        self,
        market: MarketSim,
        account_id: str = "PAPER-001",
        starting_cash: float = 100_000.0,
    ) -> None:
        """
        Initialize account engine.

        Args:
            market: MarketSim instance for pricing.
            account_id: Unique account identifier.
            starting_cash: Initial cash balance.
        """
        self.market = market
        self.account_id = account_id
        self.cash = starting_cash
        self.starting_cash = starting_cash
        self.realized_pnl = 0.0
        self.positions: dict[str, dict] = {}  # {symbol: {"quantity": int, "averagePrice": float}}
        self.orders: dict[str, dict] = {}  # {orderId: order_dict}
        self.reserved_cash = 0.0  # Cash reserved for working LIMIT BUY orders

    def place_order(
        self,
        symbol: str,
        instruction: str,
        quantity: int,
        order_type: str,
        price: Optional[float] = None,
    ) -> dict:
        """
        Place an order.

        Args:
            symbol: Ticker symbol.
            instruction: "BUY" or "SELL".
            quantity: Number of shares.
            order_type: "MARKET" or "LIMIT".
            price: Required for LIMIT orders.

        Returns:
            Order dict with status and details.
        """
        order_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()

        # Validation
        if symbol not in self.market.symbols:
            return {
                "orderId": order_id,
                "symbol": symbol,
                "instruction": instruction,
                "quantity": quantity,
                "orderType": order_type,
                "limitPrice": price,
                "status": "REJECTED",
                "reason": f"Unknown symbol: {symbol}",
                "fillPrice": None,
                "placedAt": now,
                "filledAt": None,
            }

        if quantity <= 0:
            return {
                "orderId": order_id,
                "symbol": symbol,
                "instruction": instruction,
                "quantity": quantity,
                "orderType": order_type,
                "limitPrice": price,
                "status": "REJECTED",
                "reason": "Quantity must be positive",
                "fillPrice": None,
                "placedAt": now,
                "filledAt": None,
            }

        if order_type == "LIMIT" and price is None:
            return {
                "orderId": order_id,
                "symbol": symbol,
                "instruction": instruction,
                "quantity": quantity,
                "orderType": order_type,
                "limitPrice": price,
                "status": "REJECTED",
                "reason": "LIMIT orders require a price",
                "fillPrice": None,
                "placedAt": now,
                "filledAt": None,
            }

        if instruction == "SELL":
            held = self.positions.get(symbol, {}).get("quantity", 0)
            if held < quantity:
                return {
                    "orderId": order_id,
                    "symbol": symbol,
                    "instruction": instruction,
                    "quantity": quantity,
                    "orderType": order_type,
                    "limitPrice": price,
                    "status": "REJECTED",
                    "reason": f"Insufficient shares: holding {held}, trying to sell {quantity}",
                    "fillPrice": None,
                    "placedAt": now,
                    "filledAt": None,
                }

        # Try immediate fill for MARKET orders or marketable LIMIT orders
        quote = self.market.quote(symbol)
        if "error" in quote:
            return {
                "orderId": order_id,
                "symbol": symbol,
                "instruction": instruction,
                "quantity": quantity,
                "orderType": order_type,
                "limitPrice": price,
                "status": "REJECTED",
                "reason": quote["error"],
                "fillPrice": None,
                "placedAt": now,
                "filledAt": None,
            }

        order_dict = {
            "orderId": order_id,
            "symbol": symbol,
            "instruction": instruction,
            "quantity": quantity,
            "orderType": order_type,
            "limitPrice": price,
            "status": "WORKING",
            "reason": None,
            "fillPrice": None,
            "placedAt": now,
            "filledAt": None,
        }

        # Check if marketable
        is_marketable = False
        fill_price = None

        if order_type == "MARKET":
            is_marketable = True
            if instruction == "BUY":
                fill_price = quote["ask"] * (1 + 0.0001)  # 1 bp slippage
            else:  # SELL
                fill_price = quote["bid"] * (1 - 0.0001)  # 1 bp slippage against trader
        elif order_type == "LIMIT":
            if instruction == "BUY":
                if price >= quote["ask"]:
                    is_marketable = True
                    fill_price = quote["ask"]
            else:  # SELL
                if price <= quote["bid"]:
                    is_marketable = True
                    fill_price = quote["bid"]

        if is_marketable:
            # Execute fill
            result = self._execute_fill(order_dict, fill_price)
            if result.get("error"):
                order_dict["status"] = "REJECTED"
                order_dict["reason"] = result["error"]
            else:
                order_dict["status"] = "FILLED"
                order_dict["fillPrice"] = fill_price
                order_dict["filledAt"] = datetime.now(timezone.utc).isoformat()
        else:
            # Working order
            if instruction == "BUY":
                # Reserve cash for this limit order
                required_cash = quantity * price
                if self.cash + self.reserved_cash < required_cash:
                    return {
                        "orderId": order_id,
                        "symbol": symbol,
                        "instruction": instruction,
                        "quantity": quantity,
                        "orderType": order_type,
                        "limitPrice": price,
                        "status": "REJECTED",
                        "reason": "Insufficient cash (including reserved)",
                        "fillPrice": None,
                        "placedAt": now,
                        "filledAt": None,
                    }
                self.reserved_cash += required_cash

        self.orders[order_id] = order_dict
        return order_dict

    def _execute_fill(self, order_dict: dict, fill_price: float) -> dict:
        """
        Execute a fill on an order.

        Returns:
            {} if successful, {"error": "reason"} if failed.
        """
        symbol = order_dict["symbol"]
        quantity = order_dict["quantity"]
        instruction = order_dict["instruction"]

        if instruction == "BUY":
            cost = quantity * fill_price
            if self.cash < cost:
                return {"error": "Insufficient cash"}
            self.cash -= cost

            if symbol in self.positions:
                # Update average price
                pos = self.positions[symbol]
                old_qty = pos["quantity"]
                old_avg = pos["averagePrice"]
                new_qty = old_qty + quantity
                new_avg = (old_qty * old_avg + quantity * fill_price) / new_qty
                pos["quantity"] = new_qty
                pos["averagePrice"] = new_avg
            else:
                self.positions[symbol] = {"quantity": quantity, "averagePrice": fill_price}

        else:  # SELL
            proceeds = quantity * fill_price
            self.cash += proceeds

            pos = self.positions[symbol]
            old_qty = pos["quantity"]
            old_avg = pos["averagePrice"]

            realized_gain = (fill_price - old_avg) * quantity
            self.realized_pnl += realized_gain

            pos["quantity"] -= quantity
            if pos["quantity"] == 0:
                del self.positions[symbol]

        return {}

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a WORKING order."""
        if order_id not in self.orders:
            return {"error": "Order not found"}

        order = self.orders[order_id]
        if order["status"] != "WORKING":
            return {"error": f"Cannot cancel order with status {order['status']}"}

        # Release reserved cash if BUY LIMIT
        if order["instruction"] == "BUY" and order["orderType"] == "LIMIT":
            reserved = order["quantity"] * order["limitPrice"]
            self.reserved_cash -= reserved

        order["status"] = "CANCELLED"
        order["filledAt"] = datetime.now(timezone.utc).isoformat()

        return {"status": "CANCELLED"}

    def get_order(self, order_id: str) -> Optional[dict]:
        """Get a single order by ID."""
        return self.orders.get(order_id)

    def list_orders(self) -> list[dict]:
        """List all orders."""
        return list(self.orders.values())

    def process_open_orders(self) -> None:
        """
        Check all WORKING orders and fill any that have become marketable.
        """
        for order_id, order in list(self.orders.items()):
            if order["status"] != "WORKING":
                continue

            symbol = order["symbol"]
            quote = self.market.quote(symbol)
            if "error" in quote:
                continue

            is_marketable = False
            fill_price = None

            if order["instruction"] == "BUY":
                if order["limitPrice"] >= quote["ask"]:
                    is_marketable = True
                    fill_price = quote["ask"]
            else:  # SELL
                if order["limitPrice"] <= quote["bid"]:
                    is_marketable = True
                    fill_price = quote["bid"]

            if is_marketable:
                # Release reserved cash first
                if order["instruction"] == "BUY":
                    reserved = order["quantity"] * order["limitPrice"]
                    self.reserved_cash -= reserved

                result = self._execute_fill(order, fill_price)
                if not result.get("error"):
                    order["status"] = "FILLED"
                    order["fillPrice"] = fill_price
                    order["filledAt"] = datetime.now(timezone.utc).isoformat()

    def snapshot(self) -> dict:
        """
        Get account snapshot.

        Returns:
            {
                "accountId",
                "cash",
                "positions": [{"symbol","quantity","averagePrice","marketValue","unrealizedPnl"}],
                "equity",
                "realizedPnl",
                "timestamp"
            }
        """
        positions_list = []
        total_market_value = 0.0

        for symbol, pos in self.positions.items():
            quote = self.market.quote(symbol)
            if "error" not in quote:
                market_price = quote["last"]
                market_value = pos["quantity"] * market_price
                unrealized_pnl = (market_price - pos["averagePrice"]) * pos["quantity"]
                total_market_value += market_value

                positions_list.append(
                    {
                        "symbol": symbol,
                        "quantity": pos["quantity"],
                        "averagePrice": round(pos["averagePrice"], 2),
                        "marketValue": round(market_value, 2),
                        "unrealizedPnl": round(unrealized_pnl, 2),
                    }
                )

        equity = self.cash + total_market_value

        return {
            "accountId": self.account_id,
            "cash": round(self.cash, 2),
            "positions": positions_list,
            "equity": round(equity, 2),
            "realizedPnl": round(self.realized_pnl, 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
