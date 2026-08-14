"""Synthetic news and message board feed for paper trading."""

import random
import uuid
from datetime import datetime, timezone
from typing import Optional

from .market import MarketSim, _stable_hash


class NewsFeed:
    """Generates deterministic synthetic news and message board posts."""

    # Wire news templates (professional tone) - positive
    WIRE_POSITIVE = [
        "{SYM} shares rise as analysts raise targets",
        "{SYM} in focus after upbeat guidance chatter",
        "{SYM} gains traction on strong earnings outlook",
        "{SYM} rallies on positive sector momentum",
        "{SYM} extends gains on technical strength",
        "{SYM} benefits from macro tailwinds",
        "{SYM} hits new highs on institutional buying",
        "{SYM} momentum builds on solid fundamentals",
    ]

    # Wire news templates (professional tone) - negative
    WIRE_NEGATIVE = [
        "{SYM} shares slip as analysts cut targets",
        "{SYM} in focus after disappointing guidance chatter",
        "{SYM} pressured on weak earnings outlook",
        "{SYM} declines amid sector headwinds",
        "{SYM} retreats on technical weakness",
        "{SYM} hit by macro headwinds",
        "{SYM} tests support levels on selling pressure",
        "{SYM} momentum fades on weak fundamentals",
    ]

    # Board posts (retail tone) - positive
    BOARD_POSITIVE = [
        "{SYM} to the moon, loading calls",
        "{SYM} is printing money, huge moves ahead",
        "{SYM} breakout imminent, this is it",
        "{SYM} going parabolic, get in before liftoff",
        "{SYM} undervalued at these levels, major upside",
        "{SYM} the next big winner, bullish setup",
        "{SYM} insane volume, something is brewing",
        "{SYM} technical setup looks beautiful",
        "{SYM} shorts are in trouble, squeeze incoming",
        "{SYM} diamond hands will be rewarded",
    ]

    # Board posts (retail tone) - negative
    BOARD_NEGATIVE = [
        "{SYM} is cooked, bag holders coping",
        "{SYM} death spiral confirmed, exit now",
        "{SYM} looking weak, pain ahead",
        "{SYM} bears in control, downside clear",
        "{SYM} fundamentals are terrible, stay away",
        "{SYM} this is the top, sell everything",
        "{SYM} red flag after red flag, avoid",
        "{SYM} overvalued garbage, gonna crash",
        "{SYM} insiders dumping, something is wrong",
        "{SYM} pump and dump scheme exposed",
    ]

    def __init__(self, market: MarketSim, seed: int = 42) -> None:
        """
        Initialize news feed.

        Args:
            market: MarketSim instance for accessing prices and sim time.
            seed: Random seed for deterministic generation.
        """
        self.market = market
        self.seed = seed
        self.rng = random.Random(seed)

        # Track generated items: {symbol: [item_dict]}
        self._items: dict[str, list[dict]] = {s: [] for s in market.symbols}

        # Track hidden alignment: {item_id: alignment_string}
        self._truth: dict[str, str] = {}

        # Track last processed sim time to generate new items lazily
        self._last_sim_time: Optional[float] = None

    def _get_sim_time_seconds(self) -> float:
        """Get current simulated time in seconds from market."""
        return self.market._get_sim_time_seconds()

    def _generate_items_for_symbol(self, symbol: str, sim_time_seconds: float) -> None:
        """
        Generate new items for a symbol if enough time has elapsed.

        Items are generated lazily: roughly one per symbol per 15 sim-minutes on average,
        with occasional bursts of 3-5 items.
        """
        # On first call, generate initial items
        if self._last_sim_time is None:
            self._last_sim_time = sim_time_seconds - 3600  # Pretend an hour has passed

        time_delta_seconds = sim_time_seconds - self._last_sim_time

        # 15 sim-minutes = 900 seconds
        # Expected items per symbol: time_delta / 900
        # Add some randomness for bursts
        base_expected = time_delta_seconds / 900.0

        # Use seeded RNG to decide number of items deterministically
        self.rng.seed(_stable_hash(symbol) ^ int(sim_time_seconds) ^ self.seed)

        # Probability-based generation with burst potential
        num_items = 0
        burst_roll = self.rng.random()

        if burst_roll < 0.15:  # 15% chance of burst
            num_items = self.rng.randint(3, 5)
        else:
            # Generate items based on expected value
            # Add base items + chance of one more
            num_items = int(base_expected)
            remainder = base_expected - num_items
            if self.rng.random() < remainder:
                num_items += 1

        # Generate items
        sim_timestamp = self.market._sim_timestamp()

        for _ in range(num_items):
            # Determine source: 40% wire, 60% board
            source = "wire" if self.rng.random() < 0.4 else "board"

            # Get current price direction for bias
            quote = self.market.quote(symbol)
            if "error" not in quote:
                current_price = quote["last"]
            else:
                current_price = self.market.start_prices.get(symbol, 100.0)

            # Check recent price direction (look at history if available)
            price_direction = self._get_price_direction(symbol)  # "up", "down", or "neutral"

            # Determine tone based on source
            if source == "wire":
                # Wire: mostly noise, but some aligned/inverted
                alignment_roll = self.rng.random()
                if alignment_roll < 0.3:
                    alignment = "aligned"
                    tone = "positive" if price_direction == "up" else "negative"
                elif alignment_roll < 0.7:
                    alignment = "noise"
                    tone = "positive" if self.rng.random() < 0.5 else "negative"
                else:
                    alignment = "inverted"
                    tone = "negative" if price_direction == "up" else "positive"

                templates = self.WIRE_POSITIVE if tone == "positive" else self.WIRE_NEGATIVE
            else:
                # Board: momentum-chasing, leans with recent direction
                alignment_roll = self.rng.random()
                if alignment_roll < 0.3:
                    alignment = "aligned"
                    tone = "positive" if price_direction == "up" else "negative"
                elif alignment_roll < 0.7:
                    alignment = "noise"
                    tone = "positive" if self.rng.random() < 0.5 else "negative"
                else:
                    alignment = "inverted"
                    tone = "negative" if price_direction == "up" else "positive"

                templates = self.BOARD_POSITIVE if tone == "positive" else self.BOARD_NEGATIVE

            # Generate headline
            template = self.rng.choice(templates)
            headline = template.format(SYM=symbol)

            # Create item
            item_id = str(uuid.uuid4())
            item = {
                "id": item_id,
                "symbol": symbol,
                "source": source,
                "headline": headline,
                "published": sim_timestamp.isoformat(),
            }

            # Store item
            self._items[symbol].append(item)
            self._truth[item_id] = alignment

    def _get_price_direction(self, symbol: str) -> str:
        """Determine recent price direction from market history."""
        history = self.market.price_history(symbol, days=1, interval_minutes=60)

        if "error" in history or not history.get("candles"):
            return "neutral"

        candles = history["candles"]
        if len(candles) < 2:
            return "neutral"

        # Compare last few candles to earlier ones
        recent_close = candles[-1]["close"]
        earlier_close = candles[0]["close"]

        if recent_close > earlier_close * 1.02:  # Up more than 2%
            return "up"
        elif recent_close < earlier_close * 0.98:  # Down more than 2%
            return "down"
        else:
            return "neutral"

    def items(self, symbol: str, limit: int = 10) -> list[dict]:
        """
        Get most recent items for a symbol.

        Args:
            symbol: Ticker symbol.
            limit: Maximum number of items to return.

        Returns:
            List of items, newest first (no alignment key included).
        """
        if symbol not in self.market.symbols:
            return []

        # Generate new items if time has elapsed
        sim_time_now = self._get_sim_time_seconds()
        self._generate_items_for_symbol(symbol, sim_time_now)
        self._last_sim_time = sim_time_now

        # Get items for this symbol, newest first
        all_items = self._items.get(symbol, [])

        # Sort by published time (newest first)
        sorted_items = sorted(all_items, key=lambda x: x["published"], reverse=True)

        # Return up to limit items without alignment
        result = []
        for item in sorted_items[:limit]:
            # Create a copy without the alignment (not stored in item anyway)
            result.append(
                {
                    "id": item["id"],
                    "symbol": item["symbol"],
                    "source": item["source"],
                    "headline": item["headline"],
                    "published": item["published"],
                }
            )

        return result

    def all_items(self, symbols: list[str], limit: int = 10) -> dict:
        """
        Get items for multiple symbols.

        Args:
            symbols: List of ticker symbols.
            limit: Maximum number of items per symbol.

        Returns:
            Dict mapping symbols to their item lists.
        """
        result = {}
        for symbol in symbols:
            result[symbol] = self.items(symbol, limit)
        return result
