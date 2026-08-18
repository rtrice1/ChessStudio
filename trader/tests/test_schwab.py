"""Tests for the Schwab client: translators, OCC mapping, order lockout."""
import io
import unittest
import urllib.error
from unittest import mock

from agent.schwab import (OrdersDisabled, SchwabClient, SchwabError,
                          TokenStore, authorize_url, extract_code,
                          occ_to_schwab)


def client_with(responses: dict) -> SchwabClient:
    """SchwabClient whose HTTP layer is replaced by canned responses
    keyed on path prefix."""
    c = SchwabClient.__new__(SchwabClient)
    c.tokens = mock.Mock()
    c.account_id = ""
    c._account_hash = None

    def fake_get(path, params=None):
        for prefix, payload in responses.items():
            if path.startswith(prefix):
                return payload
        raise AssertionError(f"unexpected GET {path}")
    c._get = fake_get
    return c


class TestOccMapping(unittest.TestCase):
    def test_root_padded_to_six(self):
        self.assertEqual(occ_to_schwab("AAPL260821C00190000"),
                         "AAPL  260821C00190000")
        self.assertEqual(occ_to_schwab("GOOGL260821P00140000"),
                         "GOOGL 260821P00140000")

    def test_stock_symbols_pass_through(self):
        self.assertEqual(occ_to_schwab("AAPL"), "AAPL")


class TestQuoteTranslation(unittest.TestCase):
    def test_quotes_translated_to_our_shape(self):
        c = client_with({"/marketdata/v1/quotes": {
            "AAPL": {"quote": {"bidPrice": 189.98, "askPrice": 190.02,
                               "lastPrice": 190.00}}}})
        out = c.quotes(["AAPL"])
        self.assertEqual(out["AAPL"]["bid"], 189.98)
        self.assertEqual(out["AAPL"]["last"], 190.00)
        self.assertIn("timestamp", out["AAPL"])

    def test_missing_quote_is_error_shaped(self):
        c = client_with({"/marketdata/v1/quotes": {}})
        self.assertIn("error", c.quotes(["ZZZQ"])["ZZZQ"])

    def test_option_quote_maps_padded_symbol_back(self):
        c = client_with({"/marketdata/v1/quotes": {
            "AAPL  260821C00190000": {"quote": {"bidPrice": 3.0,
                                                "askPrice": 3.2,
                                                "lastPrice": 3.1}}}})
        out = c.quotes(["AAPL260821C00190000"])
        self.assertEqual(out["AAPL260821C00190000"]["ask"], 3.2)


class TestHistoryAndChain(unittest.TestCase):
    def test_price_history_epoch_ms_to_iso(self):
        c = client_with({"/marketdata/v1/pricehistory": {
            "candles": [{"open": 1, "high": 2, "low": 0.5, "close": 1.5,
                         "volume": 100, "datetime": 1755172800000}]}})
        out = c.price_history("AAPL")
        self.assertEqual(len(out["candles"]), 1)
        self.assertTrue(out["candles"][0]["datetime"].startswith("2025-08-14"))

    def test_price_history_requests_through_now(self):
        # Without endDate Schwab ends at the PREVIOUS close — intraday
        # indicators would run on yesterday's bars (2026-08-18 regression).
        seen = {}
        c = client_with({})

        def spy_get(path, params=None):
            seen.update(params or {})
            return {"candles": []}
        c._get = spy_get
        c.price_history("AAPL")
        self.assertIn("endDate", seen)
        import time as _time
        self.assertAlmostEqual(seen["endDate"] / 1000.0, _time.time(),
                               delta=60)

    def test_chain_translated(self):
        c = client_with({"/marketdata/v1/chains": {
            "status": "SUCCESS",
            "callExpDateMap": {"2026-08-21:7": {
                "190.0": [{"symbol": "AAPL  260821C00190000",
                           "strikePrice": 190.0, "bid": 3.0, "ask": 3.2,
                           "last": 3.1, "delta": 0.52, "gamma": 0.04,
                           "theta": -0.15, "vega": 0.11, "volatility": 28.5}]}},
            "putExpDateMap": {}}})
        chain = c.chain("AAPL")
        self.assertEqual(chain["expiry"], "2026-08-21")
        call = chain["calls"][0]
        self.assertEqual(call["contractSymbol"], "AAPL260821C00190000")
        self.assertEqual(call["delta"], 0.52)
        self.assertEqual(chain["puts"], [])

    def test_chain_missing_delta_sentinel_becomes_none(self):
        c = client_with({"/marketdata/v1/chains": {
            "callExpDateMap": {"2026-08-21:7": {
                "190.0": [{"symbol": "AAPL  260821C00190000",
                           "strikePrice": 190.0, "bid": 3.0, "ask": 3.2,
                           "delta": -999.0}]}},
            "putExpDateMap": {}}})
        self.assertIsNone(c.chain("AAPL")["calls"][0]["delta"])


class TestAccountTranslation(unittest.TestCase):
    def test_account_snapshot_shape(self):
        c = client_with({
            "/trader/v1/accounts/accountNumbers": [
                {"accountNumber": "12345678", "hashValue": "HASH1"}],
            "/trader/v1/accounts/HASH1": {"securitiesAccount": {
                "accountNumber": "12345678",
                "currentBalances": {"cashAvailableForTrading": 25_000.0,
                                    "liquidationValue": 31_000.0},
                "positions": [{"longQuantity": 10, "shortQuantity": 0,
                               "averagePrice": 150.0, "marketValue": 1600.0,
                               "currentDayProfitLoss": 100.0,
                               "instrument": {"symbol": "AAPL"}}]}}})
        acct = c.account()
        self.assertEqual(acct["cash"], 25_000.0)
        self.assertEqual(acct["equity"], 31_000.0)
        self.assertEqual(acct["positions"][0]["symbol"], "AAPL")
        self.assertEqual(acct["positions"][0]["quantity"], 10)


class TestOrderLockout(unittest.TestCase):
    def test_orders_raise_unconditionally(self):
        c = client_with({})
        with self.assertRaises(OrdersDisabled):
            c.place_order("AAPL", "BUY", 1, "MARKET")
        with self.assertRaises(OrdersDisabled):
            c.cancel_order("any-id")
        self.assertEqual(c.list_orders(), [])


class TestTokenStore(unittest.TestCase):
    def test_no_tokens_raises_with_instruction(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            store = TokenStore(path=os.path.join(tmp, "none.json"))
            with self.assertRaises(Exception) as caught:
                store.access_token()
            self.assertIn("schwab auth", str(caught.exception))

    def test_status_never_carries_secrets(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict("os.environ", {"SCHWAB_APP_KEY": "KEY",
                                            "SCHWAB_APP_SECRET": "SECRET"}):
            store = TokenStore(path=os.path.join(tmp, "none.json"))
            status = store.status()
            self.assertEqual(status, {"configured": True, "has_tokens": False,
                                      "refresh_age_days": None})
            self.assertNotIn("SECRET", str(status))

    def test_token_error_surfaces_schwab_body(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            store = TokenStore(path=os.path.join(tmp, "t.json"))
            err = urllib.error.HTTPError(
                "https://api.schwabapi.com/v1/oauth/token", 400, "Bad Request",
                None, io.BytesIO(b'{"error":"invalid_grant"}'))
            with mock.patch("agent.schwab.urllib.request.urlopen",
                            side_effect=err):
                with self.assertRaises(SchwabError) as caught:
                    store.exchange_code("dead-code", "https://127.0.0.1")
            self.assertIn("HTTP 400", str(caught.exception))
            self.assertIn("invalid_grant", str(caught.exception))


class TestAuthHelpers(unittest.TestCase):
    def test_authorize_url_carries_client_id_and_redirect(self):
        url = authorize_url("KEY123", "https://127.0.0.1")
        self.assertIn("client_id=KEY123", url)
        self.assertIn("redirect_uri=https%3A%2F%2F127.0.0.1", url)

    def test_extract_code_from_full_redirect_url(self):
        # Schwab codes end in '@', percent-encoded in the address bar.
        self.assertEqual(
            extract_code("https://127.0.0.1/?code=C0.abc%40&session=xyz"),
            "C0.abc@")

    def test_extract_code_accepts_bare_code(self):
        self.assertEqual(extract_code("  C0.abc@ "), "C0.abc@")

    def test_extract_code_empty_when_missing(self):
        self.assertEqual(extract_code("https://127.0.0.1/?session=xyz"), "")
        self.assertEqual(extract_code(""), "")


if __name__ == "__main__":
    unittest.main()
