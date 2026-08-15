"""Tests for the dashboard state assembly and endpoints."""
import json
import os
import tempfile
import threading
import unittest
import urllib.request

from agent.dashboard import StateAssembler, create_server


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

    def test_halt_flag(self):
        open(os.path.join(self.data, "HALT"), "w").close()
        self.assertTrue(StateAssembler(self.data, self.desk).assemble()["halted"])


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

            state = json.loads(urllib.request.urlopen(base + "/state").read())
            self.assertIn("account", state)

            with urllib.request.urlopen(base + "/events") as stream:
                line = stream.readline().decode()
                self.assertTrue(line.startswith("data: "))
                payload = json.loads(line[6:])
                self.assertIn("positions", payload)
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
