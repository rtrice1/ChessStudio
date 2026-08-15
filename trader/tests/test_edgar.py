"""Tests for SEC EDGAR 8-K filing scanner."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from agent import edgar


class TestParseFeed(unittest.TestCase):
    """Tests for parse_feed."""

    def test_parse_well_formed_entries(self):
        """Parse feed with well-formed 8-K entries."""
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>8-K - APPLE INC (0000320193) (Filer)</title>
    <updated>2026-08-15T10:30:00Z</updated>
    <link href="https://www.sec.gov/cgi-bin/viewer?action=view&amp;cik=320193&amp;accession_number=0000320193-26-000001&amp;xbrl_type=v" />
  </entry>
  <entry>
    <title>8-K - MICROSOFT CORP (0000789019) (Filer)</title>
    <updated>2026-08-15T11:00:00Z</updated>
    <link href="https://www.sec.gov/cgi-bin/viewer?action=view&amp;cik=789019&amp;accession_number=0000789019-26-000002&amp;xbrl_type=v" />
  </entry>
</feed>"""
        entries = edgar.parse_feed(xml)
        self.assertEqual(len(entries), 2)

        # Check first entry
        self.assertEqual(entries[0]["form"], "8-K")
        self.assertEqual(entries[0]["company"], "APPLE INC")
        self.assertEqual(entries[0]["cik"], "0000320193")
        self.assertEqual(entries[0]["updated"], "2026-08-15T10:30:00Z")
        self.assertIn("https://www.sec.gov/cgi-bin/viewer", entries[0]["link"])

        # Check second entry
        self.assertEqual(entries[1]["form"], "8-K")
        self.assertEqual(entries[1]["company"], "MICROSOFT CORP")
        self.assertEqual(entries[1]["cik"], "0000789019")

    def test_parse_malformed_title(self):
        """Parse entry with title that doesn't match regex."""
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Some weird title without structure</title>
    <updated>2026-08-15T12:00:00Z</updated>
    <link href="https://example.com/filing1" />
  </entry>
</feed>"""
        entries = edgar.parse_feed(xml)
        self.assertEqual(len(entries), 1)
        # Malformed title yields empty strings, but entry is still returned.
        self.assertEqual(entries[0]["form"], "")
        self.assertEqual(entries[0]["company"], "")
        self.assertEqual(entries[0]["cik"], "")
        self.assertEqual(entries[0]["title"], "Some weird title without structure")

    def test_parse_skip_entry_without_title(self):
        """Skip entries that have no title element."""
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <updated>2026-08-15T12:00:00Z</updated>
    <link href="https://example.com/filing1" />
  </entry>
  <entry>
    <title>8-K - TEST INC (0000111111) (Filer)</title>
    <updated>2026-08-15T12:15:00Z</updated>
    <link href="https://example.com/filing2" />
  </entry>
</feed>"""
        entries = edgar.parse_feed(xml)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["company"], "TEST INC")

    def test_parse_garbage_bytes(self):
        """Parse garbage bytes returns [] without raising."""
        entries = edgar.parse_feed(b"not xml at all")
        self.assertEqual(entries, [])

    def test_parse_empty_bytes(self):
        """Parse empty bytes returns [] without raising."""
        entries = edgar.parse_feed(b"")
        self.assertEqual(entries, [])


class TestLoadTickerMap(unittest.TestCase):
    """Tests for load_ticker_map."""

    def test_load_valid_ticker_map(self):
        """Load valid company_tickers.json."""
        data = {
            "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
            "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp."},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            temp_path = f.name

        try:
            ticker_map = edgar.load_ticker_map(temp_path)
            self.assertEqual(ticker_map["0000320193"], "AAPL")
            self.assertEqual(ticker_map["0000789019"], "MSFT")
        finally:
            os.unlink(temp_path)

    def test_load_missing_file(self):
        """Load missing file returns {}."""
        ticker_map = edgar.load_ticker_map("/nonexistent/path/tickers.json")
        self.assertEqual(ticker_map, {})

    def test_load_corrupted_json(self):
        """Load corrupted JSON returns {}."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{ not valid json }")
            temp_path = f.name

        try:
            ticker_map = edgar.load_ticker_map(temp_path)
            self.assertEqual(ticker_map, {})
        finally:
            os.unlink(temp_path)

    def test_cik_zero_padded_to_10_digits(self):
        """CIKs are zero-padded to 10 digits."""
        data = {
            "0": {"cik_str": 1, "ticker": "TEST1"},
            "1": {"cik_str": 12, "ticker": "TEST2"},
            "2": {"cik_str": 320193, "ticker": "TEST3"},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            temp_path = f.name

        try:
            ticker_map = edgar.load_ticker_map(temp_path)
            self.assertEqual(ticker_map["0000000001"], "TEST1")
            self.assertEqual(ticker_map["0000000012"], "TEST2")
            self.assertEqual(ticker_map["0000320193"], "TEST3")
        finally:
            os.unlink(temp_path)


class TestFilingsForWatchlist(unittest.TestCase):
    """Tests for filings_for_watchlist."""

    def test_filter_to_watchlist(self):
        """Include only entries in watchlist."""
        entries = [
            {
                "title": "8-K - APPLE INC (0000320193) (Filer)",
                "updated": "2026-08-15T10:00:00Z",
                "link": "https://example.com/1",
                "cik": "0000320193",
                "company": "APPLE INC",
                "form": "8-K",
            },
            {
                "title": "8-K - MICROSOFT CORP (0000789019) (Filer)",
                "updated": "2026-08-15T10:30:00Z",
                "link": "https://example.com/2",
                "cik": "0000789019",
                "company": "MICROSOFT CORP",
                "form": "8-K",
            },
        ]
        ticker_map = {
            "0000320193": "AAPL",
            "0000789019": "MSFT",
        }
        watch = ["AAPL"]

        result = edgar.filings_for_watchlist(entries, ticker_map, watch)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["symbol"], "AAPL")
        self.assertEqual(result[0]["cik"], "0000320193")

    def test_filter_empty_watchlist(self):
        """Empty watchlist returns no entries."""
        entries = [
            {
                "cik": "0000320193",
                "company": "APPLE INC",
                "form": "8-K",
            },
        ]
        ticker_map = {"0000320193": "AAPL"}
        watch = []

        result = edgar.filings_for_watchlist(entries, ticker_map, watch)
        self.assertEqual(result, [])

    def test_filter_no_matching_tickers(self):
        """No matching tickers returns empty list."""
        entries = [
            {
                "cik": "0000320193",
                "company": "APPLE INC",
                "form": "8-K",
            },
        ]
        ticker_map = {"0000320193": "AAPL"}
        watch = ["MSFT", "GOOGL"]

        result = edgar.filings_for_watchlist(entries, ticker_map, watch)
        self.assertEqual(result, [])

    def test_case_insensitive_match(self):
        """Watchlist matching is case-insensitive."""
        entries = [
            {
                "cik": "0000320193",
                "company": "APPLE INC",
                "form": "8-K",
            },
        ]
        ticker_map = {"0000320193": "AAPL"}
        watch = ["aapl"]  # lowercase

        result = edgar.filings_for_watchlist(entries, ticker_map, watch)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["symbol"], "AAPL")

    def test_skip_entry_without_cik(self):
        """Skip entries that have no CIK."""
        entries = [
            {
                "cik": "",
                "company": "SOME COMPANY",
                "form": "8-K",
            },
        ]
        ticker_map = {}
        watch = ["AAPL"]

        result = edgar.filings_for_watchlist(entries, ticker_map, watch)
        self.assertEqual(result, [])


class TestScanDeduplication(unittest.TestCase):
    """Tests for scan deduplication."""

    def test_scan_dedup_on_second_call(self):
        """Second scan call doesn't re-add already-seen filings."""
        canned_feed = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>8-K - APPLE INC (0000320193) (Filer)</title>
    <updated>2026-08-15T10:00:00Z</updated>
    <link href="https://example.com/filing1" />
  </entry>
</feed>"""

        canned_ticker_map = {
            "0000320193": "AAPL",
        }

        # Create a tempdir for the test.
        with tempfile.TemporaryDirectory() as tmpdir:
            # Mock _get to return canned feed.
            with patch("agent.edgar._get") as mock_get:

                def side_effect(url, timeout=10.0):
                    if url == edgar.FEED_URL:
                        return canned_feed
                    elif url == edgar.TICKER_MAP_URL:
                        return json.dumps(
                            {
                                "0": {
                                    "cik_str": 320193,
                                    "ticker": "AAPL",
                                    "title": "Apple Inc.",
                                }
                            }
                        ).encode()
                    raise ValueError(f"Unexpected URL: {url}")

                mock_get.side_effect = side_effect

                # First call should return the filing.
                first_result = edgar.scan(["AAPL"], tmpdir)
                self.assertEqual(len(first_result), 1)
                self.assertEqual(first_result[0]["symbol"], "AAPL")

                # Verify it was written to the JSONL file.
                jsonl_path = os.path.join(tmpdir, "edgar_filings.jsonl")
                self.assertTrue(os.path.exists(jsonl_path))
                with open(jsonl_path) as f:
                    lines = f.readlines()
                self.assertEqual(len(lines), 1)

                # Second call should return empty (already seen).
                second_result = edgar.scan(["AAPL"], tmpdir)
                self.assertEqual(len(second_result), 0)

                # Verify JSONL still has only one line.
                with open(jsonl_path) as f:
                    lines = f.readlines()
                self.assertEqual(len(lines), 1)

    def test_scan_jsonl_no_duplicate_links(self):
        """JSONL file has no duplicate links."""
        canned_feed = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>8-K - APPLE INC (0000320193) (Filer)</title>
    <updated>2026-08-15T10:00:00Z</updated>
    <link href="https://example.com/filing1" />
  </entry>
  <entry>
    <title>8-K - APPLE INC (0000320193) (Filer)</title>
    <updated>2026-08-15T10:30:00Z</updated>
    <link href="https://example.com/filing1" />
  </entry>
</feed>"""

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("agent.edgar._get") as mock_get:

                def side_effect(url, timeout=10.0):
                    if url == edgar.FEED_URL:
                        return canned_feed
                    elif url == edgar.TICKER_MAP_URL:
                        return json.dumps(
                            {
                                "0": {
                                    "cik_str": 320193,
                                    "ticker": "AAPL",
                                    "title": "Apple Inc.",
                                }
                            }
                        ).encode()
                    raise ValueError(f"Unexpected URL: {url}")

                mock_get.side_effect = side_effect

                # Scan once.
                result = edgar.scan(["AAPL"], tmpdir)

                # Only one filing should be returned (second is dedup'd).
                self.assertEqual(len(result), 1)

                # Verify JSONL has only one line.
                jsonl_path = os.path.join(tmpdir, "edgar_filings.jsonl")
                with open(jsonl_path) as f:
                    lines = [line.strip() for line in f if line.strip()]
                self.assertEqual(len(lines), 1)

                # Verify the entry has seen_at field.
                entry = json.loads(lines[0])
                self.assertIn("seen_at", entry)
                self.assertIsInstance(entry["seen_at"], (int, float))

    def test_scan_network_failure_returns_empty(self):
        """Network failure returns [] without raising."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("agent.edgar._get") as mock_get:
                # Simulate network failure.
                mock_get.side_effect = Exception("Network error")

                result = edgar.scan(["AAPL"], tmpdir)
                self.assertEqual(result, [])

    def test_scan_empty_watchlist(self):
        """Scan with empty watchlist returns []."""
        canned_feed = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>8-K - APPLE INC (0000320193) (Filer)</title>
    <updated>2026-08-15T10:00:00Z</updated>
    <link href="https://example.com/filing1" />
  </entry>
</feed>"""

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("agent.edgar._get") as mock_get:

                def side_effect(url, timeout=10.0):
                    if url == edgar.FEED_URL:
                        return canned_feed
                    elif url == edgar.TICKER_MAP_URL:
                        return json.dumps({}).encode()
                    raise ValueError(f"Unexpected URL: {url}")

                mock_get.side_effect = side_effect

                result = edgar.scan([], tmpdir)
                self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
