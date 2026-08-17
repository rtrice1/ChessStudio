"""Smoke test for the setup checkpoint — it must never crash, on any
machine state, because it's the first thing a fresh instance runs."""
import os
import unittest

from agent.checkpoint import run

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestCheckpoint(unittest.TestCase):
    def test_runs_and_reports(self):
        ok, todo = run(BASE)
        self.assertTrue(any("checkout" in line for line in ok))
        # every todo line names a concrete command or file
        for line in todo:
            self.assertTrue(any(tok in line for tok in
                                ("python", "pip", "export", "bash", "data/")),
                            line)

    def test_handoff_doc_exists_and_points_here(self):
        path = os.path.join(BASE, "HANDOFF.md")
        self.assertTrue(os.path.exists(path))
        text = open(path, encoding="utf-8").read()
        self.assertIn("agent.checkpoint", text)
        self.assertIn("place_order", text)   # the hard lines travel with it


if __name__ == "__main__":
    unittest.main()
