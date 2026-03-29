"""
Market microstructure data — funding rates, open interest, long/short ratio,
and multi-asset correlations.

All sources are free public APIs with no authentication required:
  - Funding Rate + OI : Kraken Futures public REST (futures.kraken.com)
  - Long/Short Ratio  : Binance Futures public REST (fapi.binance.com)
  - Correlations      : Yahoo Finance daily closes via yfinance

Data is cached with a 10-minute TTL to avoid hammering external APIs.

Role in system: Called once per standard cycle and passed as additional
context to the confluence scorer and NarrativeContext builder.

Dependencies: requests, yfinance, pydantic, loguru
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import requests
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

# ── Cache ─────────────────────────────────────────────────────────────────────
_CACHE_TTL = 600  # 10 minutes
_cache: dict[str, tuple[Any, float]] = {}


def _cache_get(key: str) -> Any | None:
    if key in _cache:
        val, ts = _cache[key]
        if time.time() - ts < _CACHE_TTL:
            return val
    return None


def _cache_set(key: str, val: Any) -> None:
    _cache[key] = (val, time.time())


# ── Models ────────────────────────────────────────────────────────────────────

class FundingData(BaseModel):
    """Kraken Futures funding rate and open interest snapshot."""

    model_config = ConfigDict(frozen=True)

    funding_rate: float = Field(description="Current perpetual funding rate (e.g. 0.0001 = 0.01%).")
    funding_rate_annualised: float = Field(description="Funding rate annualised (× 3 × 365).")
    open_interest_usd: float = Field(ge=0.0, description="Total open interest in USD.")
    oi_change_24h_pct: float = Field(description="24h OI change as percentage.")
    sentiment: str = Field(description="'bullish_squeeze', 'bearish_squeeze', or 'neutral'.")
    fetched_at: str


class LongShortRatio(BaseModel):
    """Global long/short account ratio from Binance Futures."""

    model_config = ConfigDict(frozen=True)

    long_pct: float = Field(ge=0.0, le=100.0)
    short_pct: float = Field(ge=0.0, le=100.0)
    ratio: float = Field(ge=0.0, description="long_pct / short_pct.")
    bias: str = Field(description="'long_heavy', 'short_heavy', or 'balanced'.")
    fetched_at: str


class MarketCorrelations(BaseModel):
    """Rolling 20-day Pearson correlation of BTC daily returns vs other assets."""

    model_config = ConfigDict(frozen=True)

    btc_eth: float = Field(description="BTC vs ETH correlation (-1 to 1).")
    btc_dxy: float = Field(description="BTC vs DXY (dollar index) correlation.")
    btc_gold: float = Field(description="BTC vs Gold correlation.")
    btc_sp500: float = Field(description="BTC vs S&P 500 correlation.")
    risk_regime: str = Field(description="'risk_on', 'risk_off', or 'decorrelated'.")
    divergence_signal: bool = Field(description="True when BTC/ETH correlation drops below 0.5 (divergence alert).")
    fetched_at: str


class MicrostructureSnapshot(BaseModel):
    """Aggregated microstructure data passed to confluence and narrative layers."""

    model_config = ConfigDict(frozen=False)

    funding: FundingData | None = None
    long_short: LongShortRatio | None = None
    correlations: MarketCorrelations | None = None


# ── Funding Rate + Open Interest (Kraken Futures) ─────────────────────────────

_KRAKEN_FUTURES_TICKERS = "https://futures.kraken.com/derivatives/api/v3/tickers"
# Kraken Futures instrument names for BTC perpetual
_KRAKEN_PERP_MAP = {
    "BTCUSD": "PF_XBTUSD",
    "ETHUSD": "PF_ETHUSD",
    "XBTUSD": "PF_XBTUSD",
}


def get_funding_data(pair: str = "BTCUSD") -> FundingData:
    """
    Fetch funding rate and open interest from Kraken Futures public API.

    Falls back to a neutral placeholder if the API is unreachable.

    Args:
        pair: Trading pair, e.g. 'BTCUSD'.

    Returns:
        FundingData snapshot.
    """
    cache_key = f"funding_{pair}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    instrument = _KRAKEN_PERP_MAP.get(pair.upper(), "PF_XBTUSD")

    try:
        resp = requests.get(_KRAKEN_FUTURES_TICKERS, timeout=8)
        resp.raise_for_status()
        data = resp.json()

        ticker = next(
            (t for t in data.get("tickers", []) if t.get("symbol") == instrument),
            None,
        )

        if ticker is None:
            raise ValueError(f"Symbol {instrument} not found in Kraken Futures tickers")

        funding_rate = float(ticker.get("fundingRate", 0.0))
        funding_rate_annualised = funding_rate * 3 * 365  # 3 payments/day × 365 days

        oi_now = float(ticker.get("openInterest", 0.0))
        oi_24h_ago = float(ticker.get("openInterest24h", oi_now) or oi_now)
        oi_change = ((oi_now - oi_24h_ago) / oi_24h_ago * 100) if oi_24h_ago else 0.0

        # Classify funding sentiment
        if funding_rate > 0.0005:  # >0.05% — longs paying heavily, squeeze risk
            sentiment = "bullish_squeeze"
        elif funding_rate < -0.0005:  # <-0.05% — shorts paying heavily, squeeze risk
            sentiment = "bearish_squeeze"
        else:
            sentiment = "neutral"

        result = FundingData(
            funding_rate=round(funding_rate, 6),
            funding_rate_annualised=round(funding_rate_annualised, 4),
            open_interest_usd=round(oi_now, 0),
            oi_change_24h_pct=round(oi_change, 2),
            sentiment=sentiment,
            fetched_at=datetime.now(UTC).isoformat(),
        )

        _cache_set(cache_key, result)
        logger.debug(
            f"Funding: rate={funding_rate:.5f} ann={funding_rate_annualised:.2%} "
            f"OI=${oi_now:,.0f} ({oi_change:+.1f}% 24h) → {sentiment}"
        )
        return result

    except Exception as exc:
        logger.warning(f"Funding data unavailable ({exc}), using neutral placeholder")
        result = FundingData(
            funding_rate=0.0,
            funding_rate_annualised=0.0,
            open_interest_usd=0.0,
            oi_change_24h_pct=0.0,
            sentiment="neutral",
            fetched_at=datetime.now(UTC).isoformat(),
        )
        _cache_set(cache_key, result)
        return result


# ── Long/Short Ratio (Binance Futures public) ─────────────────────────────────

_BINANCE_LS_URL = "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
_BINANCE_SYMBOL_MAP = {
    "BTCUSD": "BTCUSDT",
    "ETHUSD": "ETHUSDT",
    "XBTUSD": "BTCUSDT",
}


def get_long_short_ratio(pair: str = "BTCUSD") -> LongShortRatio:
    """
    Fetch global long/short account ratio from Binance Futures (no API key needed).

    Falls back to a balanced placeholder if unavailable.

    Args:
        pair: Trading pair, e.g. 'BTCUSD'.

    Returns:
        LongShortRatio snapshot.
    """
    cache_key = f"ls_ratio_{pair}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    symbol = _BINANCE_SYMBOL_MAP.get(pair.upper(), "BTCUSDT")

    try:
        params = {"symbol": symbol, "period": "5m", "limit": 1}
        resp = requests.get(_BINANCE_LS_URL, params=params, timeout=8)
        resp.raise_for_status()
        data = resp.json()

        if not data:
            raise ValueError("Empty response from Binance L/S ratio endpoint")

        row = data[0]
        long_pct = float(row.get("longAccount", 0.5)) * 100
        short_pct = 100.0 - long_pct
        ratio = long_pct / short_pct if short_pct > 0 else 1.0

        if ratio > 1.3:
            bias = "long_heavy"   # > 56.5% longs — potential reversal fuel
        elif ratio < 0.77:
            bias = "short_heavy"  # < 43.5% longs
        else:
            bias = "balanced"

        result = LongShortRatio(
            long_pct=round(long_pct, 2),
            short_pct=round(short_pct, 2),
            ratio=round(ratio, 3),
            bias=bias,
            fetched_at=datetime.now(UTC).isoformat(),
        )
        _cache_set(cache_key, result)
        logger.debug(f"L/S ratio: long={long_pct:.1f}% short={short_pct:.1f}% ({bias})")
        return result

    except Exception as exc:
        logger.warning(f"L/S ratio unavailable ({exc}), using balanced placeholder")
        result = LongShortRatio(
            long_pct=50.0,
            short_pct=50.0,
            ratio=1.0,
            bias="balanced",
            fetched_at=datetime.now(UTC).isoformat(),
        )
        _cache_set(cache_key, result)
        return result


# ── Multi-asset Correlations (yfinance) ───────────────────────────────────────

def get_correlations(lookback_days: int = 20) -> MarketCorrelations:
    """
    Compute rolling Pearson correlations of BTC daily returns vs ETH, DXY, Gold, S&P500.

    Uses yfinance to fetch daily close prices. Results cached 10 minutes.
    Falls back gracefully if yfinance is unavailable.

    Args:
        lookback_days: Number of trading days for rolling correlation. Default 20.

    Returns:
        MarketCorrelations snapshot.
    """
    cache_key = f"correlations_{lookback_days}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    try:
        import yfinance as yf
        import pandas as pd

        # Fetch lookback_days + buffer to ensure we have enough clean data
        fetch_days = lookback_days + 10
        tickers = {
            "BTC-USD":  "btc",
            "ETH-USD":  "eth",
            "GC=F":     "gold",    # Gold futures
            "DX-Y.NYB": "dxy",     # US Dollar Index
            "ES=F":     "sp500",   # S&P 500 E-mini futures
        }

        closes = {}
        for ticker_sym, label in tickers.items():
            try:
                df = yf.download(
                    ticker_sym,
                    period=f"{fetch_days}d",
                    interval="1d",
                    progress=False,
                    auto_adjust=True,
                )
                if not df.empty and "Close" in df.columns:
                    closes[label] = df["Close"].dropna()
            except Exception:
                pass

        if "btc" not in closes or len(closes["btc"]) < 5:
            raise ValueError("BTC price data unavailable from yfinance")

        # Align to common dates and compute returns
        combined = pd.DataFrame(closes).dropna()
        returns = combined.pct_change().dropna().tail(lookback_days)

        def corr(a: str, b: str) -> float:
            if a in returns.columns and b in returns.columns:
                return round(float(returns[a].corr(returns[b])), 3)
            return 0.0

        btc_eth   = corr("btc", "eth")
        btc_dxy   = corr("btc", "dxy")
        btc_gold  = corr("btc", "gold")
        btc_sp500 = corr("btc", "sp500")

        # Risk regime classification
        # Risk-on: BTC positively correlated with equities (moves with risk assets)
        # Risk-off: BTC negatively correlated with DXY (dollar up = BTC down)
        if btc_sp500 > 0.4 and btc_dxy < -0.3:
            risk_regime = "risk_on"
        elif btc_dxy > 0.3 or btc_sp500 < -0.3:
            risk_regime = "risk_off"
        else:
            risk_regime = "decorrelated"

        # Divergence alert: BTC/ETH correlation below 0.5 is unusual — alt-season or BTC-specific event
        divergence_signal = btc_eth < 0.5

        result = MarketCorrelations(
            btc_eth=btc_eth,
            btc_dxy=btc_dxy,
            btc_gold=btc_gold,
            btc_sp500=btc_sp500,
            risk_regime=risk_regime,
            divergence_signal=divergence_signal,
            fetched_at=datetime.now(UTC).isoformat(),
        )
        _cache_set(cache_key, result)
        logger.debug(
            f"Correlations: ETH={btc_eth:.2f} DXY={btc_dxy:.2f} "
            f"Gold={btc_gold:.2f} SP500={btc_sp500:.2f} → {risk_regime}"
        )
        return result

    except Exception as exc:
        logger.warning(f"Correlations unavailable ({exc}), using zero placeholders")
        result = MarketCorrelations(
            btc_eth=0.0,
            btc_dxy=0.0,
            btc_gold=0.0,
            btc_sp500=0.0,
            risk_regime="decorrelated",
            divergence_signal=False,
            fetched_at=datetime.now(UTC).isoformat(),
        )
        _cache_set(cache_key, result)
        return result


def get_microstructure(pair: str = "BTCUSD") -> MicrostructureSnapshot:
    """
    Fetch all microstructure data in one call.

    Individual sources fail gracefully — a failure in one does not prevent
    the others from being returned.

    Args:
        pair: Trading pair.

    Returns:
        MicrostructureSnapshot with up to 3 data sources populated.
    """
    funding = None
    long_short = None
    correlations = None

    try:
        funding = get_funding_data(pair)
    except Exception as exc:
        logger.warning(f"Funding data fetch failed: {exc}")

    try:
        long_short = get_long_short_ratio(pair)
    except Exception as exc:
        logger.warning(f"L/S ratio fetch failed: {exc}")

    try:
        correlations = get_correlations()
    except Exception as exc:
        logger.warning(f"Correlations fetch failed: {exc}")

    return MicrostructureSnapshot(
        funding=funding,
        long_short=long_short,
        correlations=correlations,
    )
