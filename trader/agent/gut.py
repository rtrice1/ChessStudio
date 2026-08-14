"""Gut feel, made honest.

A trader's gut is compressed experience: this open *smells like* a fade
day because a hundred remembered days that felt like this faded. I don't
get the hundred days for free — but the desk can accumulate them. Every
session ends by recording the day's fingerprint (agent/daytype.py), its
label, and what actually happened. Every session begins, once the opening
range is in, by asking: which remembered days does today resemble, and
how did those go?

The output is a hunch, and it's treated like one: it can shade the plan
(risk down on suspected chop), it can never override the risk gate, and
it always states how many days of experience it rests on. A gut built on
four days says so.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone

FEATURE_KEYS = ["open_vol_ratio", "efficiency", "breadth",
                "avg_abs_return", "vwap_above_frac"]
# Rough scale of each feature's typical spread, for distance normalization.
FEATURE_SCALE = {"open_vol_ratio": 1.5, "efficiency": 0.3, "breadth": 0.35,
                 "avg_abs_return": 0.02, "vwap_above_frac": 0.35}


class Gut:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def record_day(self, features: dict, day_type: str, outcome: dict) -> dict:
        entry = {"ts": datetime.now(timezone.utc).isoformat(),
                 "features": {k: features.get(k) for k in FEATURE_KEYS},
                 "day_type": day_type, "outcome": outcome}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return entry

    def days(self) -> list[dict]:
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

    def similar_days(self, features: dict, k: int = 5) -> list[tuple[float, dict]]:
        scored = []
        for day in self.days():
            past = day.get("features") or {}
            dist_sq, n = 0.0, 0
            for key in FEATURE_KEYS:
                a, b = features.get(key), past.get(key)
                if a is None or b is None:
                    continue
                dist_sq += ((a - b) / FEATURE_SCALE[key]) ** 2
                n += 1
            if n == 0:
                continue
            similarity = 1.0 / (1.0 + math.sqrt(dist_sq / n))
            scored.append((similarity, day))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored[:k]

    def hunch(self, features: dict, k: int = 5) -> dict:
        """What does today smell like? Similarity-weighted vote of the k
        most similar remembered days."""
        similar = self.similar_days(features, k)
        if not similar:
            return {"suspected_day_type": None, "confidence": 0.0,
                    "expected_pnl_pct": None, "based_on": 0,
                    "note": "no comparable days remembered yet — "
                            "there is no gut to trust, so trust the plan"}

        weights: dict[str, float] = {}
        total = 0.0
        pnl_weighted = 0.0
        pnl_weight_total = 0.0
        for similarity, day in similar:
            weights[day["day_type"]] = weights.get(day["day_type"], 0.0) + similarity
            total += similarity
            pnl = (day.get("outcome") or {}).get("pnl_pct")
            if pnl is not None:
                pnl_weighted += similarity * pnl
                pnl_weight_total += similarity

        suspected = max(weights, key=lambda t: weights[t])
        confidence = weights[suspected] / total if total else 0.0
        expected = (pnl_weighted / pnl_weight_total) if pnl_weight_total else None
        return {
            "suspected_day_type": suspected,
            "confidence": round(confidence, 2),
            "expected_pnl_pct": round(expected, 6) if expected is not None else None,
            "based_on": len(similar),
            "note": (f"smells like {suspected} "
                     f"({weights[suspected]:.2f}/{total:.2f} of similarity weight, "
                     f"{len(similar)} remembered days"
                     + (f", those days averaged {expected:+.2%}" if expected is not None else "")
                     + ")"),
        }
