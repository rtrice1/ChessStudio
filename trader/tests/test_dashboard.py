"""Tests for the dashboard state assembly and endpoints."""
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

from agent.dashboard import StateAssembler, create_server, trades_view
from agent.schwab import SchwabError, TokenStore


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f)


def append_jsonl(path, entries):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


class TestStateAssembler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, "data")
        self.desk = os.path.join(self.tmp.name, "desk")
        os.makedirs(self.data)
        os.makedirs(self.desk)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_dirs_yield_sane_state(self):
        state = StateAssembler(self.data, self.desk).assemble()
        self.assertEqual(state["positions"], [])
        self.assertEqual(state["events"], [])
        self.assertFalse(state["halted"])

    def test_state_reads_snapshot_ledger_and_desk(self):
        write_json(os.path.join(self.data, "latest.json"), {
            "timestamp": "t1",
            "account": {"equity": 100_500.0, "cash": 90_000.0,
                        "positions": [{"symbol": "AAPL", "quantity": 10},
                                      {"symbol": "MSFT", "quantity": 0}]},
            "alerts": [{"symbol": "AAPL", "kind": "big_move"}]})
        append_jsonl(os.path.join(self.data, "ledger.jsonl"), [
            {"ts": "2026-08-15T14:00:00", "kind": "fill", "symbol": "AAPL",
             "action": "BUY", "quantity": 10},
            {"ts": "2026-08-15T14:01:00", "kind": "poll"},  # not an EVENT_KIND
            {"ts": "2026-08-15T14:02:00", "kind": "risk_reject", "reason": "cap"}])
        append_jsonl(os.path.join(self.desk, "journal.jsonl"),
                     [{"ts": "t", "kind": "note", "text": "hello"}])
        write_json(os.path.join(self.desk, "beliefs.json"),
                   {"b1": {"value": True, "reason": "r", "history": []}})

        state = StateAssembler(self.data, self.desk).assemble()
        self.assertEqual(len(state["positions"]), 1)   # zero-qty filtered
        self.assertEqual([e["kind"] for e in state["events"]],
                         ["risk_reject", "fill"])       # newest first, filtered
        self.assertEqual(state["beliefs"], {"b1": True})
        self.assertEqual(state["equity_series"][-1], ["t1", 100_500.0])

    def test_equity_series_dedupes_by_timestamp(self):
        write_json(os.path.join(self.data, "latest.json"),
                   {"timestamp": "t1", "account": {"equity": 100.0}})
        asm = StateAssembler(self.data, self.desk)
        asm.assemble()
        asm.assemble()  # same snapshot timestamp -> no duplicate point
        self.assertEqual(len(asm.equity_series), 1)

    def test_holdings_carry_live_exit_levels(self):
        write_json(os.path.join(self.data, "latest.json"), {
            "timestamp": "2026-08-18T14:40:00",
            "account": {"equity": 10_000.0, "positions": [
                {"symbol": "AAPL", "quantity": 5, "averagePrice": 311.0},
                {"symbol": "MSFT", "quantity": 0, "averagePrice": 0.0}]},
            "indicators": {"AAPL": {"atr14": 0.8}}})
        append_jsonl(os.path.join(self.data, "ledger.jsonl"), [
            {"ts": "2026-08-18T14:00:00", "kind": "live_session_start"},
            {"ts": "2026-08-18T14:39:09", "kind": "fill", "symbol": "AAPL",
             "action": "BUY", "quantity": 2, "order": {"fillPrice": 311.11}}])
        state = StateAssembler(self.data, self.desk).assemble()
        self.assertEqual(len(state["holdings"]), 1)   # zero-qty excluded
        h = state["holdings"][0]
        # same arithmetic as decide(): avg ± plan multiples × ATR
        self.assertAlmostEqual(h["stop"], 311.0 - 1.5 * 0.8)
        self.assertAlmostEqual(h["target"], 311.0 + 2.5 * 0.8)
        self.assertFalse(h["trail"])
        self.assertEqual(state["session_fills"]["AAPL"],
                         [["14:39:09", "BUY", 311.11]])

    def test_candles_pass_through_to_state(self):
        write_json(os.path.join(self.data, "latest.json"), {
            "timestamp": "t1", "account": {"equity": 10_000.0},
            "candles": {"AAPL": [["09:30", 310.0, 311.2, 309.8, 311.0, 120000],
                                 ["09:35", 311.0, 311.5, 310.6, 310.9, 80000]]}})
        state = StateAssembler(self.data, self.desk).assemble()
        self.assertEqual(len(state["candles"]["AAPL"]), 2)
        self.assertEqual(state["candles"]["AAPL"][0][0], "09:30")

    def test_holdings_respect_trail_plan(self):
        write_json(os.path.join(self.data, "day_plan.json"),
                   {"exit_style": "trail", "stop_atr": 2.0})
        write_json(os.path.join(self.data, "latest.json"), {
            "timestamp": "t1",
            "account": {"equity": 10_000.0, "positions": [
                {"symbol": "XOM", "quantity": 10, "averagePrice": 164.0}]},
            "indicators": {"XOM": {"atr14": 0.5}}})
        h = StateAssembler(self.data, self.desk).assemble()["holdings"][0]
        self.assertAlmostEqual(h["stop"], 164.0 - 2.0 * 0.5)
        self.assertIsNone(h["target"])   # no fixed target while trailing
        self.assertTrue(h["trail"])

    def test_halt_flag(self):
        open(os.path.join(self.data, "HALT"), "w").close()
        self.assertTrue(StateAssembler(self.data, self.desk).assemble()["halted"])

    def test_overlay_series_and_entry_scores(self):
        write_json(os.path.join(self.data, "latest.json"), {
            "timestamp": "2026-08-15T14:00:00",
            "account": {"equity": 100_000.0},
            "quotes": {"AAPL": {"last": 101.5}},
            "indicators": {"AAPL": {
                "vwap": 100.5, "bb_upper": 103.0, "bb_lower": 99.0,
                "range_high": 101.0, "range_low": 99.5,
                "adx": 30.0, "plus_di": 25.0, "minus_di": 10.0,
                "macd_hist": 0.4, "rel_volume": 1.6, "bb_percent_b": 0.9,
                "rsi14": 55.0, "roc10": 1.0}},
            "news": {"summary": {"AAPL": {"wire_sentiment": -2,
                                          "board_sentiment": -1}}}})
        state = StateAssembler(self.data, self.desk).assemble()
        # Overlays tick alongside the price series, on the same clock.
        self.assertEqual(state["overlay_series"]["AAPL"],
                         [["14:00:00", 100.5, 103.0, 99.0]])
        # And the live score matches the engine's ranking arithmetic.
        aapl = state["entry_scores"]["AAPL"]
        self.assertGreater(aapl["score"], 0)
        self.assertIn("trending", aapl["why"])
        self.assertIn("news -3", aapl["why"])


class TestTradesView(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = self.tmp.name
        append_jsonl(os.path.join(self.data, "ledger.jsonl"), [
            # day 1: one full round trip
            {"ts": "2026-08-18T14:38:00+00:00", "kind": "fill",
             "symbol": "XOM", "action": "BUY", "quantity": 6,
             "order": {"fillPrice": 163.90},
             "rationale": "ORB: breakout | score +2"},
            {"ts": "2026-08-18T15:10:00+00:00", "kind": "fill",
             "symbol": "XOM", "action": "SELL", "quantity": 6,
             "order": {"fillPrice": 164.40},
             "rationale": "ATR target: 164.40 >= 164.35"},
            # day 2: a loss and a lot still open
            {"ts": "2026-08-19T13:35:00+00:00", "kind": "fill",
             "symbol": "AAPL", "action": "BUY", "quantity": 3,
             "order": {"fillPrice": 316.50}, "rationale": "ORB: x"},
            {"ts": "2026-08-19T14:05:00+00:00", "kind": "fill",
             "symbol": "AAPL", "action": "SELL", "quantity": 3,
             "order": {"fillPrice": 316.00},
             "rationale": "ATR stop: 316.00 <= 316.02"},
            {"ts": "2026-08-19T14:44:00+00:00", "kind": "fill",
             "symbol": "TSLA", "action": "BUY", "quantity": 2,
             "order": {"fillPrice": 346.61}, "rationale": "ORB: y"}])

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_to_newest_day_with_open_lots(self):
        view = trades_view(self.data)
        self.assertEqual(view["dates"], ["2026-08-19", "2026-08-18"])
        self.assertEqual(view["date"], "2026-08-19")
        self.assertEqual(len(view["trips"]), 1)
        trip = view["trips"][0]
        self.assertEqual((trip["symbol"], trip["pnl"], trip["reason"]),
                         ("AAPL", -1.50, "ATR stop"))
        self.assertEqual(trip["entry_t"], "09:35")   # UTC ledger -> ET clock
        self.assertEqual(view["open"],
                         [{"symbol": "TSLA", "quantity": 2, "entry": 346.61,
                           "entry_t": "10:44"}])
        self.assertEqual(view["summary"]["pnl"], -1.50)

    def test_picking_an_older_day(self):
        view = trades_view(self.data, "2026-08-18")
        self.assertEqual(view["date"], "2026-08-18")
        self.assertEqual(view["summary"],
                         {"trips": 1, "wins": 1, "win_rate": 1.0, "pnl": 3.0})
        self.assertEqual(view["open"], [])

    def test_unknown_date_falls_back_to_newest(self):
        self.assertEqual(trades_view(self.data, "1999-01-01")["date"],
                         "2026-08-19")


class TestHttpEndpoints(unittest.TestCase):
    def test_page_state_and_sse(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, desk = os.path.join(tmp, "d"), os.path.join(tmp, "k")
            os.makedirs(data)
            os.makedirs(desk)
            server = create_server(host="127.0.0.1", port=0, data_dir=data,
                                   desk_dir=desk, interval=0.05)
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{port}"

            page = urllib.request.urlopen(base + "/").read().decode()
            self.assertIn("EventSource", page)
            self.assertIn("THE DESK", page)
            self.assertIn('id="modal"', page)     # wrap-up modal shell
            self.assertIn('id="wrapbtn"', page)

            state = json.loads(urllib.request.urlopen(base + "/state").read())
            self.assertIn("account", state)

            with urllib.request.urlopen(base + "/events") as stream:
                line = stream.readline().decode()
                self.assertTrue(line.startswith("data: "))
                payload = json.loads(line[6:])
                self.assertIn("positions", payload)
            server.shutdown()


class TestSchwabAuthEndpoints(unittest.TestCase):
    """The weekly OAuth ritual through the dashboard instead of the CLI."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = os.path.join(self.tmp.name, "d")
        desk = os.path.join(self.tmp.name, "k")
        os.makedirs(data)
        os.makedirs(desk)
        self.token_path = os.path.join(self.tmp.name, "tokens.json")
        self.server = create_server(host="127.0.0.1", port=0, data_dir=data,
                                    desk_dir=desk, interval=0.05)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.tmp.cleanup()

    def _post(self, obj: dict) -> dict:
        req = urllib.request.Request(
            self.base + "/auth/schwab", data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        return json.loads(urllib.request.urlopen(req).read())

    def test_page_has_auth_panel(self):
        page = urllib.request.urlopen(self.base + "/").read().decode()
        self.assertIn("Schwab connection", page)
        self.assertIn("/auth/schwab", page)
        self.assertIn("Trade log", page)
        trades = json.loads(
            urllib.request.urlopen(self.base + "/trades").read())
        self.assertEqual(trades["dates"], [])   # empty ledger, sane shape

    def test_status_unconfigured(self):
        with mock.patch.dict(os.environ, {
                "SCHWAB_APP_KEY": "", "SCHWAB_APP_SECRET": "",
                "SCHWAB_TOKEN_PATH": self.token_path}):
            st = json.loads(
                urllib.request.urlopen(self.base + "/auth/schwab").read())
        self.assertFalse(st["configured"])
        self.assertFalse(st["has_tokens"])
        self.assertIsNone(st["authorize_url"])

    def test_status_configured_shows_url_but_never_the_secret(self):
        with mock.patch.dict(os.environ, {
                "SCHWAB_APP_KEY": "KEY123", "SCHWAB_APP_SECRET": "SECRET456",
                "SCHWAB_TOKEN_PATH": self.token_path}):
            raw = urllib.request.urlopen(self.base + "/auth/schwab").read().decode()
        st = json.loads(raw)
        self.assertTrue(st["configured"])
        self.assertIn("client_id=KEY123", st["authorize_url"])
        self.assertNotIn("SECRET456", raw)

    def test_exchange_rejects_paste_without_code(self):
        with mock.patch.dict(os.environ, {
                "SCHWAB_APP_KEY": "K", "SCHWAB_APP_SECRET": "S",
                "SCHWAB_TOKEN_PATH": self.token_path}):
            out = self._post({"redirect_url": "https://127.0.0.1/"})
        self.assertFalse(out["ok"])
        self.assertIn("no ?code=", out["error"])

    def test_exchange_passes_decoded_code_to_the_store(self):
        with mock.patch.dict(os.environ, {
                "SCHWAB_APP_KEY": "K", "SCHWAB_APP_SECRET": "S",
                "SCHWAB_TOKEN_PATH": self.token_path,
                "SCHWAB_REDIRECT_URI": "https://127.0.0.1"}), \
             mock.patch.object(TokenStore, "exchange_code",
                               return_value={}) as exchanged:
            out = self._post(
                {"redirect_url": "https://127.0.0.1/?code=C0.abc%40"})
        self.assertTrue(out["ok"])
        exchanged.assert_called_once_with("C0.abc@", "https://127.0.0.1")

    def test_exchange_failure_reports_schwab_error(self):
        with mock.patch.dict(os.environ, {
                "SCHWAB_APP_KEY": "K", "SCHWAB_APP_SECRET": "S",
                "SCHWAB_TOKEN_PATH": self.token_path}), \
             mock.patch.object(
                 TokenStore, "exchange_code",
                 side_effect=SchwabError("token request failed: HTTP 400 "
                                         '{"error":"invalid_grant"}')):
            out = self._post(
                {"redirect_url": "https://127.0.0.1/?code=expired"})
        self.assertFalse(out["ok"])
        self.assertIn("invalid_grant", out["error"])


if __name__ == "__main__":
    unittest.main()
