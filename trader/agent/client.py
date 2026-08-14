"""Broker API client for paper trading."""

import json
import time
import urllib.error
import urllib.request
from typing import Any


class BrokerError(Exception):
    """Broker API error."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"BrokerError {status_code}: {body}")


class BrokerClient:
    """HTTP client for broker API."""

    def __init__(self, base_url: str, account_id: str = "PAPER-001", timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.account_id = account_id
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        retry_count: int = 0,
    ) -> Any:
        """Make HTTP request with retry logic for idempotent GETs."""
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json"}

        if data is not None:
            body = json.dumps(data).encode("utf-8")
        else:
            body = None

        req = urllib.request.Request(url, data=body, headers=headers, method=method)

        max_retries = 3 if method == "GET" else 0
        retry_delay = 0.5

        while retry_count <= max_retries:
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    response_data = response.read().decode("utf-8")
                    return json.loads(response_data) if response_data else {}
            except urllib.error.HTTPError as e:
                error_body = e.read().decode("utf-8", errors="replace")
                try:
                    error_json = json.loads(error_body)
                except json.JSONDecodeError:
                    error_json = {"error": error_body}
                raise BrokerError(e.code, json.dumps(error_json))
            except (urllib.error.URLError, TimeoutError) as e:
                if retry_count < max_retries:
                    retry_count += 1
                    time.sleep(retry_delay)
                    continue
                raise

        return {}

    def quotes(self, symbols: list[str]) -> dict:
        """Get quotes for symbols."""
        if not symbols:
            return {}
        symbols_str = ",".join(symbols)
        path = f"/v1/marketdata/quotes?symbols={symbols_str}"
        return self._request("GET", path)

    def price_history(self, symbol: str, days: int = 5, interval: int = 5) -> dict:
        """Get price history for symbol."""
        path = f"/v1/marketdata/pricehistory?symbol={symbol}&days={days}&interval={interval}"
        return self._request("GET", path)

    def account(self) -> dict:
        """Get account snapshot."""
        path = f"/v1/accounts/{self.account_id}"
        return self._request("GET", path)

    def place_order(
        self,
        symbol: str,
        instruction: str,
        quantity: int,
        order_type: str = "MARKET",
        price: float | None = None,
    ) -> dict:
        """Place an order."""
        body: dict[str, Any] = {
            "symbol": symbol,
            "instruction": instruction,
            "quantity": quantity,
            "orderType": order_type,
        }
        if price is not None:
            body["price"] = price
        path = f"/v1/accounts/{self.account_id}/orders"
        return self._request("POST", path, body)

    def list_orders(self) -> list:
        """List all orders."""
        path = f"/v1/accounts/{self.account_id}/orders"
        result = self._request("GET", path)
        return result if isinstance(result, list) else []

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an order."""
        path = f"/v1/accounts/{self.account_id}/orders/{order_id}"
        return self._request("DELETE", path)

    def news(self, symbols: list[str], limit: int = 10) -> dict:
        """Get news for symbols."""
        if not symbols:
            return {}
        symbols_str = ",".join(symbols)
        path = f"/v1/marketdata/news?symbols={symbols_str}&limit={limit}"
        return self._request("GET", path)
