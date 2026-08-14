"""Backfill the trader's gut memory with simulated market days.

The backfill tool constructs synthetic days using MarketSim, labels them
with day_features and classify_day, and records them into the Gut so it
builds pattern memory without waiting years for real market experience.
"""
import argparse
import json
import os
from typing import Optional

from agent.daytype import day_features, classify_day
from agent.gut import Gut
from mockschwab.market import MarketSim


def backfill_day(seed: int, symbols: Optional[list[str]] = None) -> Optional[dict]:
    """Construct one deterministic synthetic day and return its fingerprint.

    Args:
        seed: Random seed for the MarketSim.
        symbols: List of symbols to simulate. If None, uses MarketSim defaults.

    Returns:
        Dict with keys "seed", "features", "classification" if a day could be
        classified, or None if day_features could not be computed.
    """
    sim = MarketSim(seed=seed, symbols=symbols)

    # Use the sim's symbol list if none provided
    symbols_to_use = symbols if symbols is not None else sim.symbols

    # Pull one day of 5-minute candles for each symbol using the deterministic day_candles method
    candles_by_symbol = {}
    for symbol in symbols_to_use:
        try:
            candles = sim.day_candles(symbol)
            candles_by_symbol[symbol] = candles
        except KeyError:
            # Symbol not in the sim
            continue

    # Compute day features across all symbols
    features = day_features(candles_by_symbol)
    if features is None:
        return None

    # Classify the day
    classification = classify_day(features)

    return {
        "seed": seed,
        "features": features,
        "classification": classification,
    }


def run_backfill(
    gut: Gut,
    n_days: int,
    start_seed: int = 10_000,
    symbols: Optional[list[str]] = None,
    progress_every: int = 100,
) -> dict:
    """Backfill n_days of synthetic market history into the Gut.

    Args:
        gut: Gut instance to record days into.
        n_days: Number of synthetic days to generate.
        start_seed: First seed value (incremented for each day).
        symbols: List of symbols to simulate. If None, uses MarketSim defaults.
        progress_every: Print progress every N days.

    Returns:
        Dict with keys "recorded", "skipped", "type_counts" showing the
        backfill summary.
    """
    recorded = 0
    skipped = 0
    type_counts = {}

    for i in range(n_days):
        seed = start_seed + i
        result = backfill_day(seed, symbols)

        if result is None:
            skipped += 1
            continue

        features = result["features"]
        classification = result["classification"]
        day_type = classification["day_type"]

        # Record with source marker to distinguish from real trades
        outcome = {
            "pnl_pct": None,
            "source": "sim_backfill",
            "seed": seed,
        }
        gut.record_day(features, day_type, outcome)

        recorded += 1
        type_counts[day_type] = type_counts.get(day_type, 0) + 1

        if (i + 1) % progress_every == 0:
            print(f"Backfilled {i + 1}/{n_days} days ({recorded} recorded, {skipped} skipped)")

    return {
        "recorded": recorded,
        "skipped": skipped,
        "type_counts": type_counts,
    }


def main():
    """Command-line interface for backfilling the Gut."""
    parser = argparse.ArgumentParser(
        description="Backfill trader's gut memory with simulated market days.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=500,
        help="Number of synthetic days to generate (default: 500).",
    )
    parser.add_argument(
        "--start-seed",
        type=int,
        default=10_000,
        help="First seed value (default: 10000).",
    )
    parser.add_argument(
        "--desk-dir",
        type=str,
        default=None,
        help="Path to desk_state directory (default: trader/desk_state).",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100,
        help="Print progress every N days (default: 100).",
    )

    args = parser.parse_args()

    # Determine desk directory
    if args.desk_dir is None:
        # Resolve relative to this file's location
        this_dir = os.path.dirname(os.path.abspath(__file__))
        args.desk_dir = os.path.join(this_dir, "..", "desk_state")

    # Create Gut at desk_state/day_memory.jsonl
    gut_path = os.path.join(args.desk_dir, "day_memory.jsonl")
    gut = Gut(gut_path)

    # Run backfill
    summary = run_backfill(
        gut,
        n_days=args.days,
        start_seed=args.start_seed,
        progress_every=args.progress_every,
    )

    # Compute fractions for each day type
    recorded = summary["recorded"]
    fractions = {}
    for day_type, count in summary["type_counts"].items():
        fractions[day_type] = round(count / recorded, 4) if recorded > 0 else 0.0

    # Print summary as pretty JSON
    output = {
        "recorded": summary["recorded"],
        "skipped": summary["skipped"],
        "type_counts": summary["type_counts"],
        "type_fractions": fractions,
        "gut_path": gut_path,
        "total_days_in_gut": len(gut.days()),
    }

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
