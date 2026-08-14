"""
Persistent-memory component for agentic trading system.
Maintains durable state across agent invocations.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path


class Desk:
    """
    Durable state for trading agent across invocations.

    Root directory contains:
    - journal.jsonl: append-only log of events/notes
    - beliefs.json: current beliefs with history
    - identity.md: optional identity prose
    """

    def __init__(self, root: str):
        """
        Initialize Desk with root directory.
        Creates root and parents if they don't exist.

        Args:
            root: Path to desk state directory
        """
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

        self.journal_path = self.root / "journal.jsonl"
        self.beliefs_path = self.root / "beliefs.json"
        self.identity_path = self.root / "identity.md"

    def _now_iso(self) -> str:
        """Return current UTC time in ISO-8601 format."""
        return datetime.now(timezone.utc).isoformat()

    def journal_append(self, kind: str, payload: dict) -> dict:
        """
        Append entry to append-only journal.

        Args:
            kind: Type of journal entry
            payload: Dictionary of entry data

        Returns:
            The complete entry including ts and kind
        """
        entry = {
            "ts": self._now_iso(),
            "kind": kind,
            **payload
        }

        # Append as JSON line
        with open(self.journal_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())

        return entry

    def journal_entries(
        self, kind: str | None = None, limit: int | None = None
    ) -> list[dict]:
        """
        Read all journal entries, optionally filtered by kind.

        Args:
            kind: Optional filter by entry kind
            limit: If given, return only the last `limit` entries (most recent last)

        Returns:
            List of journal entries, sorted by time
        """
        if not self.journal_path.exists():
            return []

        entries = []
        with open(self.journal_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if kind is None or entry.get("kind") == kind:
                        entries.append(entry)
                except json.JSONDecodeError:
                    # Skip corrupt lines
                    continue

        # Apply limit to last N entries if specified
        if limit is not None:
            entries = entries[-limit:]

        return entries

    def note(self, text: str, tags: list[str] | None = None) -> dict:
        """
        Convenience method to append a note to journal.

        Args:
            text: Note text
            tags: Optional list of tags

        Returns:
            The journal entry
        """
        return self.journal_append("note", {
            "text": text,
            "tags": tags or []
        })

    def set_belief(self, key: str, value, reason: str) -> None:
        """
        Set or update a belief, maintaining history.

        Pushes previous {value, reason, updated} onto history list
        (capped at 20 entries, oldest dropped first).

        Args:
            key: Belief key
            value: New belief value
            reason: Reason for belief
        """
        beliefs = self._load_beliefs()
        now = self._now_iso()

        # If belief exists, push previous value to history
        if key in beliefs:
            prev = beliefs[key]
            history = prev.get("history", [])
            # Push previous entry onto history
            history.append({
                "value": prev.get("value"),
                "reason": prev.get("reason"),
                "updated": prev.get("updated")
            })
            # Cap history at 20, drop oldest first
            if len(history) > 20:
                history = history[-20:]
        else:
            history = []

        beliefs[key] = {
            "value": value,
            "reason": reason,
            "updated": now,
            "history": history
        }

        self._save_beliefs(beliefs)

    def get_belief(self, key: str, default=None):
        """
        Get current value of a belief.

        Args:
            key: Belief key
            default: Default value if belief doesn't exist

        Returns:
            Current belief value or default
        """
        beliefs = self._load_beliefs()
        if key in beliefs:
            return beliefs[key].get("value")
        return default

    def beliefs(self) -> dict:
        """
        Get all current beliefs as {key: value} mapping.

        Returns:
            Dictionary of all beliefs with current values only
        """
        all_beliefs = self._load_beliefs()
        return {key: val["value"] for key, val in all_beliefs.items()}

    def _load_beliefs(self) -> dict:
        """Load beliefs.json, return empty dict if doesn't exist."""
        if not self.beliefs_path.exists():
            return {}
        try:
            with open(self.beliefs_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def _save_beliefs(self, beliefs: dict) -> None:
        """
        Atomically save beliefs to beliefs.json.
        Uses temp file + os.replace for atomicity.
        """
        temp_path = self.beliefs_path.with_suffix(".tmp")
        with open(temp_path, "w") as f:
            json.dump(beliefs, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, self.beliefs_path)

    def load_context(self, journal_limit: int = 25) -> dict:
        """
        Load all context needed for fresh agent instance on wake.

        Args:
            journal_limit: Number of recent journal entries to include

        Returns:
            Dictionary with keys:
            - identity: Contents of identity.md or None
            - beliefs: All current beliefs as {key: value}
            - recent_journal: Last N journal entries
            - journal_size: Total number of entries in journal
        """
        # Load identity if it exists
        identity = None
        if self.identity_path.exists():
            try:
                with open(self.identity_path, "r") as f:
                    identity = f.read()
            except IOError:
                pass

        # Count total journal entries
        all_entries = self.journal_entries()
        journal_size = len(all_entries)

        # Get recent entries
        recent_journal = self.journal_entries(limit=journal_limit)

        return {
            "identity": identity,
            "beliefs": self.beliefs(),
            "recent_journal": recent_journal,
            "journal_size": journal_size
        }

    def wake_summary(self, journal_limit: int = 10) -> str:
        """
        Generate human/LLM-readable summary for fresh agent instance.

        Format:
        - Identity text (if any)
        - Beliefs as "- key: value (reason)"
        - Recent journal entries as "- [ts] kind: <compact json>"

        Args:
            journal_limit: Number of recent journal entries to include

        Returns:
            Multi-line string summary
        """
        lines = []

        # Add identity if present
        if self.identity_path.exists():
            try:
                with open(self.identity_path, "r") as f:
                    identity_text = f.read().strip()
                    if identity_text:
                        lines.append(identity_text)
                        lines.append("")  # Blank line separator
            except IOError:
                pass

        # Add beliefs
        all_beliefs = self._load_beliefs()
        if all_beliefs:
            lines.append("Beliefs:")
            for key, info in all_beliefs.items():
                value = info.get("value")
                reason = info.get("reason", "")
                lines.append(f"  - {key}: {value} ({reason})")
            lines.append("")  # Blank line separator

        # Add recent journal entries
        recent = self.journal_entries(limit=journal_limit)
        if recent:
            lines.append("Recent journal:")
            for entry in recent:
                ts = entry.get("ts", "")
                kind = entry.get("kind", "")
                # Create compact payload (exclude ts and kind)
                payload = {k: v for k, v in entry.items() if k not in ("ts", "kind")}
                payload_str = json.dumps(payload)
                lines.append(f"  - [{ts}] {kind}: {payload_str}")

        return "\n".join(lines)
