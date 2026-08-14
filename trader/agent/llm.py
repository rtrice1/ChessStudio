"""LLM invocation layer: tiered models, hard token budgets, dry-run.

Design rules, in order:

1. **Budget is enforced in code.** Each tier has a daily output-token cap;
   when it's spent, invocations are refused and recorded, not queued. The
   ledger (data/token_ledger.jsonl) is append-only, like every other
   record on this desk.
2. **Dry-run is the default posture.** Without the `anthropic` SDK and an
   API key, every invocation writes the exact prompt it *would* have sent
   plus an estimated cost, and returns None. The mock phase runs this way
   on purpose: we measure what the harness would spend before a single
   real token is bought.
3. **The official SDK only.** Imported lazily so the rest of the system
   keeps its stdlib-only property; the box installs `anthropic` when live
   invocations are wanted.

Tiers (per SPEC/AGENTS.md): haiku watches, sonnet triages, fable decides.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

TIER_MODELS = {
    "watcher": "claude-haiku-4-5",
    "triage": "claude-sonnet-5",
    "strategist": "claude-fable-5",
}

# $/MTok (input, output) — for the ledger's cost column.
PRICES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-fable-5": (10.00, 50.00),
}

# Daily output-token caps per tier. Deliberately tight: the strategist cap
# funds three scheduled slots plus one or two escalations, no more.
DEFAULT_DAILY_CAPS = {"watcher": 20_000, "triage": 10_000, "strategist": 30_000}

# Effort per tier (skipped for haiku, which doesn't take the parameter).
TIER_EFFORT = {"triage": "low", "strategist": "high"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class TokenLedger:
    """Append-only record of every invocation: real, dry-run, or refused."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def record(self, status: str, tier: str, kind: str,
               input_tokens: int, output_tokens: int, detail: str = "") -> dict:
        model = TIER_MODELS.get(tier, "?")
        in_price, out_price = PRICES.get(model, (0.0, 0.0))
        entry = {
            "ts": _now(), "status": status, "tier": tier, "model": model,
            "kind": kind, "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "est_cost_usd": round(input_tokens / 1e6 * in_price
                                  + output_tokens / 1e6 * out_price, 6),
            "detail": detail,
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return entry

    def entries(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def spent_today(self, tier: str) -> int:
        """Output tokens spent (or dry-run-estimated) today for a tier."""
        today = _today()
        return sum(e["output_tokens"] for e in self.entries()
                   if e["tier"] == tier and e["ts"][:10] == today
                   and e["status"] in ("ok", "dry_run"))

    def summary_today(self) -> dict:
        today = _today()
        todays = [e for e in self.entries() if e["ts"][:10] == today]
        return {
            "invocations": len([e for e in todays if e["status"] in ("ok", "dry_run")]),
            "refused": len([e for e in todays if e["status"] == "budget_denied"]),
            "est_cost_usd": round(sum(e["est_cost_usd"] for e in todays), 4),
            "by_tier": {tier: self.spent_today(tier) for tier in TIER_MODELS},
        }


def estimate_tokens(text: str) -> int:
    """Crude chars/4 estimate for dry-run accounting. Real runs use the
    API's own usage numbers; this only has to be the right order of
    magnitude for budget planning."""
    return max(1, len(text) // 4)


class LLMClient:
    def __init__(self, ledger: TokenLedger,
                 caps: dict | None = None,
                 dry_run: bool | None = None,
                 dry_run_dir: str | None = None):
        self.ledger = ledger
        self.caps = caps or dict(DEFAULT_DAILY_CAPS)
        self._client = None
        if dry_run is None:
            dry_run = os.environ.get("TRADER_LLM_LIVE", "") != "1"
        if not dry_run:
            try:
                import anthropic
                self._client = anthropic.Anthropic()
            except Exception:
                self._client = None  # no SDK/key -> dry-run regardless
        self.dry_run = self._client is None
        self.dry_run_dir = dry_run_dir

    def invoke(self, tier: str, kind: str, prompt: str,
               schema: dict | None = None, max_tokens: int = 2000) -> dict | None:
        """One judgment call. Returns the parsed JSON object (when a schema
        was given), {"text": ...} otherwise, or None (dry-run / refused /
        declined). Every path leaves a ledger entry."""
        if tier not in TIER_MODELS:
            raise ValueError(f"unknown tier {tier!r}")

        est_in = estimate_tokens(prompt)
        spent = self.ledger.spent_today(tier)
        if spent + max_tokens > self.caps.get(tier, 0):
            self.ledger.record("budget_denied", tier, kind, 0, 0,
                               f"cap {self.caps.get(tier)} spent {spent}")
            return None

        if self.dry_run:
            self._save_dry_run(tier, kind, prompt, schema)
            self.ledger.record("dry_run", tier, kind, est_in, max_tokens // 2,
                               "estimated; no API call made")
            return None

        return self._call(tier, kind, prompt, schema, max_tokens)

    # --- real API path (official SDK; models per SPEC/AGENTS.md tiers) ---

    def _call(self, tier: str, kind: str, prompt: str,
              schema: dict | None, max_tokens: int) -> dict | None:
        model = TIER_MODELS[tier]
        params: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if tier in TIER_EFFORT:
            params["output_config"] = {"effort": TIER_EFFORT[tier]}
        if schema is not None:
            params.setdefault("output_config", {})["format"] = {
                "type": "json_schema", "schema": schema}

        if model == "claude-fable-5":
            # Fable: thinking is always on (no thinking param), and we opt
            # into server-side fallbacks so a classifier false-positive
            # doesn't silently cost us the day's plan.
            response = self._client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                **params,
            )
        else:
            response = self._client.messages.create(**params)

        usage = response.usage
        if response.stop_reason == "refusal":
            self.ledger.record("refusal", tier, kind,
                               usage.input_tokens, usage.output_tokens,
                               "declined by safety classifiers")
            return None
        if response.stop_reason == "max_tokens":
            self.ledger.record("truncated", tier, kind,
                               usage.input_tokens, usage.output_tokens,
                               "hit max_tokens; result discarded")
            return None

        self.ledger.record("ok", tier, kind,
                           usage.input_tokens, usage.output_tokens)
        text = next((b.text for b in response.content if b.type == "text"), "")
        if schema is not None:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                self.ledger.record("parse_error", tier, kind, 0, 0,
                                   "schema output did not parse")
                return None
        return {"text": text}

    def _save_dry_run(self, tier: str, kind: str, prompt: str,
                      schema: dict | None) -> None:
        if not self.dry_run_dir:
            return
        os.makedirs(self.dry_run_dir, exist_ok=True)
        stamp = _now().replace(":", "").replace("-", "")[:15]
        path = os.path.join(self.dry_run_dir, f"{stamp}-{tier}-{kind}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"tier": tier, "kind": kind, "model": TIER_MODELS[tier],
                       "prompt": prompt, "schema": schema,
                       "prompt_chars": len(prompt),
                       "est_input_tokens": estimate_tokens(prompt)}, f, indent=2)
