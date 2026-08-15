"""SEC EDGAR 8-K filing scanner.

Fetches and deduplicates material corporate event filings (8-K) from the SEC's
Atom feed. CIKs are mapped to ticker symbols via the company_tickers.json cache.
Filings are deduplicated by link URL in a JSONL file on disk.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any


FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include&count=40&output=atom"
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"

# User-Agent: append EDGAR_CONTACT env var if set.
_USER_AGENT_BASE = "trader-desk personal research agent (contact: set EDGAR_CONTACT env var)"
USER_AGENT = _USER_AGENT_BASE
_contact = os.environ.get("EDGAR_CONTACT")
if _contact:
    USER_AGENT = f"{_USER_AGENT_BASE.replace(' (contact: set EDGAR_CONTACT env var)', '')} (contact: {_contact})"


def _get(url: str, timeout: float = 10.0) -> bytes:
    """Fetch bytes from URL with User-Agent header. SEC requires descriptive UA."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def parse_feed(xml_bytes: bytes) -> list[dict]:
    """Parse Atom XML feed of 8-K filings.

    For each entry, extract:
    - title: filing title (e.g. "8-K - APPLE INC (0000320193) (Filer)")
    - updated: ISO timestamp from feed
    - link: href of the link element
    - cik: 10-digit zero-padded CIK extracted from title (or "")
    - company: company name extracted from title (or "")
    - form: form type extracted from title (or "")

    Entries with no title are skipped entirely. Malformed titles yield empty
    strings for cik/company/form but are still returned.

    Returns [] on parse failure (does not raise).
    """
    ns = "http://www.w3.org/2005/Atom"
    entries = []

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    for entry in root.findall(f"{{{ns}}}entry"):
        title_elem = entry.find(f"{{{ns}}}title")
        if title_elem is None or title_elem.text is None:
            # Skip entries with no title entirely.
            continue

        title = title_elem.text
        updated_elem = entry.find(f"{{{ns}}}updated")
        updated = updated_elem.text if updated_elem is not None else ""

        link_elem = entry.find(f"{{{ns}}}link")
        link = link_elem.get("href") if link_elem is not None else ""

        # Extract form, company, and CIK from title.
        # Format: "8-K - COMPANY NAME (0000000000) (Filer)"
        match = re.match(r"^([A-Z0-9\-/]*)\s*-\s*(.+?)\s*\((\d{10})\)", title)
        if match:
            form, company, cik = match.groups()
        else:
            form, company, cik = "", "", ""

        entries.append({
            "title": title,
            "updated": updated,
            "link": link,
            "cik": cik,
            "company": company,
            "form": form,
        })

    return entries


def load_ticker_map(path: str) -> dict[str, str]:
    """Load locally cached company_tickers.json.

    SEC format: {"0": {"cik_str": 320193, "ticker": "AAPL", ...}, ...}
    Returns: {cik_zero_padded_10: ticker} e.g. {"0000320193": "AAPL"}

    Returns {} if file is missing or unparseable.
    """
    if not os.path.exists(path):
        return {}

    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

    result = {}
    for key, entry in data.items():
        if isinstance(entry, dict):
            cik_str = entry.get("cik_str")
            ticker = entry.get("ticker")
            if cik_str is not None and ticker is not None:
                # Pad CIK to 10 digits.
                cik_padded = str(cik_str).zfill(10)
                result[cik_padded] = ticker

    return result


def fetch_ticker_map(path: str) -> dict[str, str]:
    """Download company_tickers.json if missing or >7 days old.

    Downloads TICKER_MAP_URL, saves to path, then loads it.
    On network failure, falls back to load_ticker_map (which may return {}).

    Returns {cik_zero_padded_10: ticker}.
    """
    needs_fetch = True
    if os.path.exists(path):
        mtime = os.path.getmtime(path)
        age_seconds = time.time() - mtime
        # 7 days = 604800 seconds
        if age_seconds < 604800:
            needs_fetch = False

    if needs_fetch:
        try:
            data = _get(TICKER_MAP_URL, timeout=10.0)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        except Exception:
            # Network failure or write error — fall back to existing file.
            pass

    return load_ticker_map(path)


def filings_for_watchlist(
    entries: list[dict],
    ticker_map: dict[str, str],
    watch: list[str],
) -> list[dict]:
    """Filter entries to watchlist tickers.

    For each entry, look up its CIK in ticker_map. If the resulting ticker
    is in watch (case-insensitive), include the entry with "symbol" key added.

    Returns list of filtered entries with "symbol" key.
    """
    watch_upper = {s.upper() for s in watch}
    result = []

    for entry in entries:
        cik = entry.get("cik", "")
        if not cik:
            continue

        ticker = ticker_map.get(cik, "").upper()
        if ticker in watch_upper:
            # Add symbol key and include.
            entry_with_symbol = dict(entry)
            entry_with_symbol["symbol"] = ticker
            result.append(entry_with_symbol)

    return result


def scan(watch: list[str], data_dir: str) -> list[dict]:
    """Fetch and deduplicate 8-K filings.

    Fetches the FEED_URL, parses 8-K entries, maps CIKs to tickers via
    cached company_tickers.json, filters to watchlist, and appends NEW filings
    (deduplicated by link URL) to edgar_filings.jsonl.

    Adds "seen_at" field (time.time()) to each new filing.

    Returns list of newly appended filings.
    On network failure, returns [] without raising.
    """
    try:
        feed_data = _get(FEED_URL, timeout=10.0)
    except Exception:
        # Network failure — return empty list without raising.
        return []

    # Parse feed.
    entries = parse_feed(feed_data)
    if not entries:
        return []

    # Fetch and map tickers.
    ticker_cache_path = os.path.join(data_dir, "company_tickers.json")
    ticker_map = fetch_ticker_map(ticker_cache_path)

    # Filter to watchlist.
    watchlist_entries = filings_for_watchlist(entries, ticker_map, watch)
    if not watchlist_entries:
        return []

    # Load existing filings to deduplicate by link.
    filings_path = os.path.join(data_dir, "edgar_filings.jsonl")
    existing_links = set()
    if os.path.exists(filings_path):
        try:
            with open(filings_path, "r") as f:
                for line in f:
                    if line.strip():
                        filing = json.loads(line)
                        existing_links.add(filing.get("link", ""))
        except (IOError, json.JSONDecodeError):
            # Corrupt or unreadable file — start fresh.
            pass

    # Append only new filings.
    new_filings = []
    os.makedirs(data_dir, exist_ok=True)
    with open(filings_path, "a") as f:
        for entry in watchlist_entries:
            link = entry.get("link", "")
            if link and link not in existing_links:
                entry["seen_at"] = time.time()
                f.write(json.dumps(entry) + "\n")
                new_filings.append(entry)
                existing_links.add(link)

    return new_filings


def main():
    """CLI entry point: fetch 8-K filings and print new ones."""
    parser = argparse.ArgumentParser(
        description="Scan SEC EDGAR for new 8-K filings."
    )
    parser.add_argument(
        "--data-dir",
        default=os.path.join(os.path.dirname(__file__), "..", "data"),
        help="Data directory for cache and output files.",
    )
    parser.add_argument(
        "--symbols",
        default="AAPL,MSFT,NVDA,GOOGL,AMZN,SPY,QQQ,TSLA,JPM,XOM",
        help="Comma-separated watchlist symbols.",
    )

    args = parser.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",")]

    new_filings = scan(symbols, args.data_dir)

    for filing in new_filings:
        symbol = filing.get("symbol", "?")
        form = filing.get("form", "?")
        updated = filing.get("updated", "?")
        title = filing.get("title", "?")
        print(f"{symbol} {form} {updated} {title}")


if __name__ == "__main__":
    main()
