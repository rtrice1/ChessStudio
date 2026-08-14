"""Options pricing and Greeks for paper trading."""

import math
import random
from datetime import datetime, timezone, timedelta
from typing import Optional
from .market import _stable_hash


def parse_occ(symbol: str) -> Optional[dict]:
    """
    Parse OCC-style option symbol.

    Format: ROOT + YYMMDD + C|P + 8-digit strike*1000
    Example: "AAPL260821C00190000" -> {"root": "AAPL", "expiry": date(2026,8,21), "put_call": "C", "strike": 190.0}

    Args:
        symbol: OCC-format symbol string.

    Returns:
        Dict with root, expiry, put_call, strike, or None if invalid.
    """
    if len(symbol) < 16:  # Min: 1 char root + 6 date + 1 C/P + 8 strike = 16
        return None

    # Find where the date starts (after the root symbol)
    # Root symbols are 1-5 uppercase letters
    root_end = 0
    for i, char in enumerate(symbol):
        if not char.isalpha():
            root_end = i
            break

    if root_end < 1 or root_end > 5:
        return None

    root = symbol[:root_end]
    rest = symbol[root_end:]

    # rest should be YYMMDD + C|P + 8 digits
    if len(rest) != 15:  # 6 (date) + 1 (C/P) + 8 (strike)
        return None

    date_str = rest[:6]
    put_call = rest[6]
    strike_str = rest[7:15]

    if put_call not in ("C", "P"):
        return None

    try:
        yy = int(date_str[:2])
        mm = int(date_str[2:4])
        dd = int(date_str[4:6])
        strike_int = int(strike_str)
    except ValueError:
        return None

    # Convert 2-digit year to 4-digit (assume 2000s)
    year = 2000 + yy

    try:
        from datetime import date
        expiry = date(year, mm, dd)
    except ValueError:
        return None

    strike = strike_int / 1000.0

    return {
        "root": root,
        "expiry": expiry,
        "put_call": put_call,
        "strike": strike,
    }


def make_occ(root: str, expiry_date, put_call: str, strike: float) -> str:
    """
    Create OCC-style option symbol.

    Args:
        root: Symbol root (e.g., "AAPL").
        expiry_date: datetime.date object.
        put_call: "C" or "P".
        strike: Strike price as float.

    Returns:
        OCC-format symbol string.
    """
    yy = expiry_date.year % 100
    mm = expiry_date.month
    dd = expiry_date.day
    strike_int = int(round(strike * 1000))

    return f"{root}{yy:02d}{mm:02d}{dd:02d}{put_call}{strike_int:08d}"


def bs_price_and_greeks(
    spot: float,
    strike: float,
    t_years: float,
    iv: float,
    put_call: str,
    r: float = 0.04
) -> dict:
    """
    Black-Scholes price and Greeks for a single option.

    Args:
        spot: Current spot price.
        strike: Strike price.
        t_years: Time to expiry in years.
        iv: Implied volatility (annualized).
        put_call: "C" for call, "P" for put.
        r: Risk-free rate (default 0.04 = 4%).

    Returns:
        {
            "price": price per share,
            "delta": delta,
            "gamma": gamma,
            "theta": theta per calendar day,
            "vega": vega per 1% IV,
        }
    """
    # Floor t_years to avoid division by zero on 0DTE near close
    t_years = max(t_years, 1 / (365 * 390))  # 390 = typical trading minutes per day

    sigma = iv

    # Standard normal CDF via error function: N(x) = 0.5*(1+erf(x/sqrt(2)))
    def norm_cdf(x):
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    def norm_pdf(x):
        return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

    # Compute d1 and d2
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma**2) * t_years) / (sigma * math.sqrt(t_years))
    d2 = d1 - sigma * math.sqrt(t_years)

    # Price
    if put_call == "C":
        price = spot * norm_cdf(d1) - strike * math.exp(-r * t_years) * norm_cdf(d2)
    else:  # Put
        price = strike * math.exp(-r * t_years) * norm_cdf(-d2) - spot * norm_cdf(-d1)

    # Intrinsic value floor
    intrinsic = max(spot - strike, 0) if put_call == "C" else max(strike - spot, 0)
    price = max(price, intrinsic)

    # Delta
    if put_call == "C":
        delta = norm_cdf(d1)
    else:
        delta = norm_cdf(d1) - 1

    # Gamma (same for calls and puts)
    gamma = norm_pdf(d1) / (spot * sigma * math.sqrt(t_years))

    # Theta (per calendar day, so divide annual theta by 365)
    if put_call == "C":
        theta_annual = (
            -spot * norm_pdf(d1) * sigma / (2 * math.sqrt(t_years))
            - r * strike * math.exp(-r * t_years) * norm_cdf(d2)
        )
    else:
        theta_annual = (
            -spot * norm_pdf(d1) * sigma / (2 * math.sqrt(t_years))
            + r * strike * math.exp(-r * t_years) * norm_cdf(-d2)
        )
    theta = theta_annual / 365.0

    # Vega (per 1% change in IV, so per 0.01)
    vega = spot * norm_pdf(d1) * math.sqrt(t_years) / 100.0

    return {
        "price": price,
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
    }


class OptionsLayer:
    """Options pricing layer using Black-Scholes."""

    def __init__(self, market, seed: int = 42):
        """
        Initialize options layer.

        Args:
            market: MarketSim instance.
            seed: Random seed for deterministic IV.
        """
        self.market = market
        self.seed = seed
        self.rng = random.Random(seed)

    def _base_iv(self, symbol: str) -> float:
        """
        Compute deterministic base implied volatility for a symbol.
        Uniform in [0.25, 0.55] annualized.
        """
        iv_rng = random.Random(_stable_hash(symbol) ^ self.seed)
        return iv_rng.uniform(0.25, 0.55)

    def _smile_iv(self, symbol: str, spot: float, strike: float) -> float:
        """
        Apply smile/skew adjustment to IV.
        iv_adjusted = base_iv * (1 + 0.5 * |K-S|/S), capped at 1.5x base.
        """
        base_iv = self._base_iv(symbol)
        moneyness = abs(strike - spot) / spot if spot > 0 else 0
        adjusted_iv = base_iv * (1 + 0.5 * moneyness)
        adjusted_iv = min(adjusted_iv, base_iv * 1.5)
        return adjusted_iv

    def expiries(self) -> list[str]:
        """
        Return list of expiry dates as ISO strings.
        Two expiries: today (0DTE) and today+7.
        """
        today = self.market._sim_timestamp().date()
        expiry_0dte = today.isoformat()
        expiry_7dte = (today + timedelta(days=7)).isoformat()
        return [expiry_0dte, expiry_7dte]

    def chain(self, symbol: str, expiry: Optional[str] = None) -> dict:
        """
        Get option chain for a symbol.

        Args:
            symbol: Underlying symbol.
            expiry: Expiry date as ISO string, or None for nearest (0DTE).

        Returns:
            {
                "symbol": underlying symbol,
                "expiry": expiry ISO string,
                "calls": [...],
                "puts": [...],
            }
            or {"error": ...} if unknown symbol/expiry.
        """
        # Validate underlying
        quote = self.market.quote(symbol)
        if "error" in quote:
            return {"error": f"Unknown symbol: {symbol}"}

        spot = quote["last"]

        # Resolve expiry
        if expiry is None:
            expiry = self.expiries()[0]

        available_expiries = self.expiries()
        if expiry not in available_expiries:
            return {"error": f"Unknown expiry: {expiry}"}

        # Check if expiry is in the past
        from datetime import date
        expiry_date = date.fromisoformat(expiry)
        today = self.market._sim_timestamp().date()
        if expiry_date < today:
            return {"error": f"Expiry in the past: {expiry}"}

        # Generate strikes around spot (11 strikes total)
        # ~1% spacing, rounded to sensible increments
        strikes = []
        for i in range(-5, 6):  # 11 strikes
            strike = spot * (1 + 0.01 * i)

            # Round to sensible increment
            if strike < 100:
                increment = 0.5
            elif strike < 500:
                increment = 1.0
            else:
                increment = 5.0

            strike = round(strike / increment) * increment
            strikes.append(strike)

        # Remove duplicates and sort
        strikes = sorted(set(strikes))

        # Ensure we have ~11 strikes
        if len(strikes) < 11:
            # Add strikes if needed
            while len(strikes) < 11:
                low = min(strikes) * 0.99
                strikes.append(round(low / increment) * increment)
                strikes = sorted(set(strikes))

        # Compute time to expiry
        now = self.market._sim_timestamp()
        expiry_dt = datetime.combine(expiry_date, datetime.min.time(), tzinfo=timezone.utc)
        # Options expire at 4 PM UTC
        expiry_dt = expiry_dt.replace(hour=16, minute=0, second=0, microsecond=0)
        t_years = (expiry_dt - now).total_seconds() / (365.25 * 24 * 3600)
        t_years = max(t_years, 1 / (365 * 390))  # Floor to avoid issues

        calls = []
        puts = []

        for strike in strikes:
            # Call
            iv_call = self._smile_iv(symbol, spot, strike)
            call_greeks = bs_price_and_greeks(spot, strike, t_years, iv_call, "C")

            call_mid = call_greeks["price"]
            half_spread = max(0.02, 0.03 * call_mid) if call_mid > 0 else 0.02
            call_bid = max(0.01, call_mid - half_spread)
            call_ask = call_mid + half_spread

            call_symbol = make_occ(symbol, expiry_date, "C", strike)
            calls.append({
                "contractSymbol": call_symbol,
                "strike": round(strike, 2),
                "putCall": "C",
                "expiry": expiry,
                "bid": round(call_bid, 2),
                "ask": round(call_ask, 2),
                "last": round(call_mid, 2),
                "delta": round(call_greeks["delta"], 4),
                "gamma": round(call_greeks["gamma"], 4),
                "theta": round(call_greeks["theta"], 4),
                "vega": round(call_greeks["vega"], 4),
                "iv": round(iv_call, 4),
            })

            # Put
            iv_put = self._smile_iv(symbol, spot, strike)
            put_greeks = bs_price_and_greeks(spot, strike, t_years, iv_put, "P")

            put_mid = put_greeks["price"]
            half_spread = max(0.02, 0.03 * put_mid) if put_mid > 0 else 0.02
            put_bid = max(0.01, put_mid - half_spread)
            put_ask = put_mid + half_spread

            put_symbol = make_occ(symbol, expiry_date, "P", strike)
            puts.append({
                "contractSymbol": put_symbol,
                "strike": round(strike, 2),
                "putCall": "P",
                "expiry": expiry,
                "bid": round(put_bid, 2),
                "ask": round(put_ask, 2),
                "last": round(put_mid, 2),
                "delta": round(put_greeks["delta"], 4),
                "gamma": round(put_greeks["gamma"], 4),
                "theta": round(put_greeks["theta"], 4),
                "vega": round(put_greeks["vega"], 4),
                "iv": round(iv_put, 4),
            })

        return {
            "symbol": symbol,
            "expiry": expiry,
            "calls": calls,
            "puts": puts,
        }

    def contract_quote(self, occ_symbol: str) -> dict:
        """
        Get quote for a specific option contract.

        Prices are PER-CONTRACT (multiplied by 100 from per-share), 100 multiplier.

        Args:
            occ_symbol: OCC-format symbol.

        Returns:
            {
                "symbol": occ_symbol,
                "bid": per-contract price,
                "ask": per-contract price,
                "last": per-contract price,
                "timestamp": ISO timestamp,
                "multiplier": 100,
            }
            or {"error": ...} if invalid/expired.
        """
        parsed = parse_occ(occ_symbol)
        if parsed is None:
            return {"error": f"Invalid OCC symbol: {occ_symbol}"}

        root = parsed["root"]
        expiry_date = parsed["expiry"]
        put_call = parsed["put_call"]
        strike = parsed["strike"]

        # Check if underlying exists
        quote = self.market.quote(root)
        if "error" in quote:
            return {"error": f"Unknown underlying: {root}"}

        spot = quote["last"]

        # Check if expiry is in the past
        today = self.market._sim_timestamp().date()
        if expiry_date < today:
            return {"error": f"Expiry in the past: {expiry_date}"}

        # Compute time to expiry
        now = self.market._sim_timestamp()
        expiry_dt = datetime.combine(expiry_date, datetime.min.time(), tzinfo=timezone.utc)
        expiry_dt = expiry_dt.replace(hour=16, minute=0, second=0, microsecond=0)
        t_years = (expiry_dt - now).total_seconds() / (365.25 * 24 * 3600)
        t_years = max(t_years, 1 / (365 * 390))

        # Price with smile
        iv = self._smile_iv(root, spot, strike)
        greeks = bs_price_and_greeks(spot, strike, t_years, iv, put_call)

        # Per-share price
        price_per_share = greeks["price"]

        # Spread
        half_spread = max(0.02, 0.03 * price_per_share) if price_per_share > 0 else 0.02
        bid_per_share = max(0.01, price_per_share - half_spread)
        ask_per_share = price_per_share + half_spread

        # Convert to per-contract (multiply by 100)
        bid = round(bid_per_share * 100, 2)
        ask = round(ask_per_share * 100, 2)
        last = round(price_per_share * 100, 2)

        return {
            "symbol": occ_symbol,
            "bid": bid,
            "ask": ask,
            "last": last,
            "timestamp": now.isoformat(),
            "multiplier": 100,
        }


class _SymbolsMatcher:
    """Smart symbols container that accepts both real symbols and parseable OCC symbols."""

    def __init__(self, real_symbols: list[str]):
        """
        Initialize matcher.

        Args:
            real_symbols: List of valid underlying symbols.
        """
        self.real_symbols = real_symbols

    def __contains__(self, symbol: str) -> bool:
        """Check if symbol is real or a valid OCC contract."""
        if symbol in self.real_symbols:
            return True
        # Check if it's a parseable OCC symbol
        return parse_occ(symbol) is not None

    def __iter__(self):
        """Iterate over real symbols only."""
        return iter(self.real_symbols)

    def __len__(self):
        """Return count of real symbols."""
        return len(self.real_symbols)


class MarketWithOptions:
    """Wrapper combining MarketSim and OptionsLayer for seamless trading."""

    def __init__(self, market, options: OptionsLayer):
        """
        Initialize market wrapper.

        Args:
            market: MarketSim instance.
            options: OptionsLayer instance.
        """
        self.market = market
        self.options = options
        # Create smart symbols matcher that accepts both real symbols and OCC contracts
        self._symbols_matcher = _SymbolsMatcher(market.symbols)

    @property
    def symbols(self):
        """Return smart symbols matcher that accepts real symbols and OCC contracts."""
        return self._symbols_matcher

    def quote(self, symbol: str) -> dict:
        """
        Get quote for symbol or OCC option contract.

        Args:
            symbol: Ticker symbol or OCC contract symbol.

        Returns:
            Quote dict with bid, ask, last, timestamp (and multiplier for options).
        """
        # Try parsing as OCC first
        parsed = parse_occ(symbol)
        if parsed is not None:
            return self.options.contract_quote(symbol)

        # Fall back to underlying market
        return self.market.quote(symbol)

    def __getattr__(self, name):
        """Delegate all other attributes to the underlying market."""
        return getattr(self.market, name)
