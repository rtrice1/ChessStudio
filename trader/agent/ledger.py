"""Append-only trade/decision ledger."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Ledger:
    """Append-only JSON-lines ledger."""

    def __init__(self, path: str):
        self.path = Path(path)
        # Create parent directories if they don't exist
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, kind: str, payload: dict) -> None:
        """Record an entry (append + flush + fsync)."""
        ts = datetime.now(timezone.utc).isoformat()
        entry = {"ts": ts, "kind": kind, **payload}

        with open(self.path, "a") as f:
            f.write(json.dumps(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def entries(self, kind: str | None = None) -> list[dict]:
        """Read all entries, optionally filtered by kind."""
        if not self.path.exists():
            return []

        entries = []
        with open(self.path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if kind is None or entry.get("kind") == kind:
                        entries.append(entry)
                except json.JSONDecodeError:
                    pass

        return entries

    def summary(self) -> dict:
        """Get summary stats."""
        all_entries = self.entries()

        # Count by kind
        kind_counts: dict[str, int] = {}
        for entry in all_entries:
            kind = entry.get("kind", "unknown")
            kind_counts[kind] = kind_counts.get(kind, 0) + 1

        # Totals for fills
        buys = 0
        sells = 0
        buy_notional = 0.0
        sell_notional = 0.0

        fill_entries = self.entries(kind="fill")
        for entry in fill_entries:
            notional = entry.get("notional", 0)
            instruction = str(entry.get("instruction") or entry.get("action") or "").upper()

            if instruction == "BUY":
                buys += 1
                buy_notional += notional
            elif instruction == "SELL":
                sells += 1
                sell_notional += notional

        return {
            "kind_counts": kind_counts,
            "buys": buys,
            "sells": sells,
            "buy_notional": buy_notional,
            "sell_notional": sell_notional,
        }
