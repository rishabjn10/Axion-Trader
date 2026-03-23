"""
Technical indicator computation engine using pandas-ta-classic.

Transforms raw OHLCV DataFrames into a fully computed ``IndicatorSnapshot``
containing all eight indicators required by the trading brain:

1. **RSI (14)** — momentum oscillator; <30 oversold, >70 overbought
2. **MACD (12/26/9)** — trend-following momentum; crossovers are signals
3. **Bollinger Bands (20, 2σ)** — volatility envelope; %B position matters
4. **VWAP** — volume-weighted average; price above = bullish, below = bearish
5. **ATR (14)** — average true range; used for stop/take-profit sizing
6. **EMA 9** — fast exponential moving average for crossover detection
7. **EMA 21** — slow EMA; 9/21 cross is a primary entry signal
8. **ADX (14)** — trend strength; >25 = trending, used in regime detection

All computations use ``pandas_ta`` (the pandas-ta-classic fork compatible with
Python 3.13). Results are returned as an immutable Pydantic model with all
floats rounded to 4 decimal places.

Role in system: Called by the main loop for each of the three timeframes
(15m, 1h, 4h). The 1h snapshot drives confluence scoring; the 4h drives
regime detection; the 15m drives the fast loop rule engine.

Dependencies: pandas, pandas_ta (pandas-ta-classic), numpy, pydantic
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
from loguru import logger

# pandas-ta may be installed under different package names depending on
# which fork the user has. Try both in order.
try:
    import pandas_ta as ta
    _TA_AVAILABLE = True
except ImportError:
    try:
        import ta as ta  # type: ignore[no-redef]
        _TA_AVAILABLE = False  # flag: use fallback computations
        logger.warning("pandas_ta not found — using manual indicator computations")
    except ImportError:
        ta = None  # type: ignore[assignment]
        _TA_AVAILABLE = False
        logger.warning("No TA library found — using manual indicator computations")
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.config.settings import settings

# Minimum candle count required — indicators need look-back warmup period
_MIN_CANDLES = 50


# ── Manual indicator implementations (used when pandas_ta unavailable) ─────────

def _ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average via pandas ewm."""
    return series.ewm(span=period, adjust=False).mean()


def _rsi_manual(series: pd.Series, period: int = 14) -> pd.Series:
    """RSI using Wilder's smoothing method."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _macd_manual(series: pd.Series, fast: int, slow: int, signal: int) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (macd_line, signal_line, histogram)."""
    ema_fast = _ema(series, fast)
    ema_slow = _ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _bbands_manual(series: pd.Series, period: int = 20, std: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Returns (lower, middle, upper, pct_b)."""
    middle = series.rolling(period).mean()
    sigma = series.rolling(period).std(ddof=0)
    upper = middle + std * sigma
    lower = middle - std * sigma
    pct_b = (series - lower) / (upper - lower).replace(0, np.nan)
    return lower, middle, upper, pct_b


def _atr_manual(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _adx_manual(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Simplified ADX (returns ADX series only)."""
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    up_move = high - prev_high
    down_move = prev_low - low

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr_s = pd.Series(tr).ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=high.index).ewm(alpha=1 / period, adjust=False).mean() / atr_s
    minus_di = 100 * pd.Series(minus_dm, index=high.index).ewm(alpha=1 / period, adjust=False).mean() / atr_s
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    return adx


class IndicatorSnapshot(BaseModel):
    """
    Point-in-time snapshot of all computed technical indicators.

    All float values are rounded to 4 decimal places for consistency and to
    avoid floating-point noise in comparisons and logging.

    Attributes:
        rsi: RSI value 0–100. <30 oversold, >70 overbought.
        macd_line: MACD line value (fast EMA − slow EMA).
        macd_signal: MACD signal line (EMA of MACD line).
        macd_histogram: MACD histogram (macd_line − macd_signal).
        macd_cross_direction: 'bullish' if histogram turned positive, 'bearish' if negative, 'none'.
        bb_upper: Bollinger Band upper boundary.
        bb_middle: Bollinger Band middle line (20-period SMA).
        bb_lower: Bollinger Band lower boundary.
        bb_pct_b: Bollinger %B — position within the band (0=lower, 1=upper, >1=above upper).
        vwap: Volume-weighted average price for the current session.
        atr: Average True Range — dollar volatility per candle.
        ema_fast: Fast EMA (period 9) value.
        ema_slow: Slow EMA (period 21) value.
        ema_cross: 'bullish' if fast crossed above slow, 'bearish' if below, 'none'.
        adx: Average Directional Index — trend strength 0–100.
        current_price: Most recent close price.
        timestamp: ISO 8601 timestamp of the most recent candle.

    Example:
        >>> snap = compute_indicators(df)
        >>> print(snap.rsi, snap.macd_cross_direction)
    """

    model_config = ConfigDict(frozen=True)

    rsi: float = Field(description="RSI value 0-100.")
    macd_line: float = Field(description="MACD line (fast - slow EMA).")
    macd_signal: float = Field(description="MACD signal line.")
    macd_histogram: float = Field(description="MACD histogram (line - signal).")
    macd_cross_direction: Literal["bullish", "bearish", "none"] = Field(
        description="MACD cross direction from previous candle."
    )
    bb_upper: float = Field(description="Bollinger Band upper boundary.")
    bb_middle: float = Field(description="Bollinger Band middle (SMA).")
    bb_lower: float = Field(description="Bollinger Band lower boundary.")
    bb_pct_b: float = Field(description="Bollinger %B position within band.")
    vwap: float = Field(description="Volume-weighted average price.")
    atr: float = Field(description="Average True Range (volatility metric).")
    ema_fast: float = Field(description=f"EMA {settings.EMA_FAST} value.")
    ema_slow: float = Field(description=f"EMA {settings.EMA_SLOW} value.")
    ema_cross: Literal["bullish", "bearish", "none"] = Field(
        description="EMA crossover direction from previous candle."
    )
    adx: float = Field(description="ADX trend strength 0-100.")
    current_price: float = Field(description="Most recent close price.")
    timestamp: str = Field(description="ISO 8601 UTC timestamp of last candle.")

    @field_validator("rsi", "adx", "bb_pct_b", mode="before")
    @classmethod
    def clamp_and_round(cls, v: float) -> float:
        """Round floats to 4 decimal places."""
        return round(float(v), 4)


def _round4(value: float) -> float:
    """Round a float to 4 decimal places, handling NaN safely."""
    if np.isnan(value) or np.isinf(value):
        return 0.0
    return round(float(value), 4)


def _safe_last(series: pd.Series) -> float:
    """Extract the last non-NaN value from a Series, returning 0.0 if all NaN."""
    valid = series.dropna()
    if valid.empty:
        return 0.0
    return float(valid.iloc[-1])


def compute_indicators(df: pd.DataFrame) -> IndicatorSnapshot:
    """
    Compute all technical indicators from an OHLCV DataFrame.

    Requires a minimum of 50 candles to ensure meaningful indicator values
    (MACD alone needs 26 + 9 warmup candles). Uses the pandas-ta-classic
    library for all computations to ensure Python 3.13 compatibility.

    The VWAP is computed as a cumulative VWAP from the start of the provided
    DataFrame window (session-style), not a rolling window, so its value
    depends on how many candles are provided.

    Args:
        df: OHLCV DataFrame with columns [timestamp, open, high, low, close, volume].
            The 'timestamp' column may be datetime or string — it will be parsed.
            Must contain at least 50 rows.

    Returns:
        IndicatorSnapshot with all indicators computed from the most recent candle.

    Raises:
        ValueError: If the DataFrame has fewer than 50 rows or missing required columns.
        RuntimeError: If pandas-ta-classic fails to compute any indicator.

    Example:
        >>> df = fetch_ohlcv('BTCUSD', 60, 200)
        >>> snap = compute_indicators(df)
        >>> print(f"RSI: {snap.rsi}, Price: {snap.current_price}")
    """
    required_cols = {"open", "high", "low", "close", "volume"}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing required columns: {missing}")

    if len(df) < _MIN_CANDLES:
        raise ValueError(
            f"Insufficient candle data: got {len(df)}, need at least {_MIN_CANDLES}. "
            f"Increase count parameter in fetch_ohlcv()."
        )

    # Work on a copy to avoid modifying the caller's DataFrame
    df = df.copy()

    # Ensure numeric types
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["open", "high", "low", "close", "volume"])
    df = df.reset_index(drop=True)

    # ── RSI ───────────────────────────────────────────────────────────────────
    if _TA_AVAILABLE:
        rsi_series = ta.rsi(df["close"], length=settings.RSI_PERIOD)
    else:
        rsi_series = _rsi_manual(df["close"], settings.RSI_PERIOD)
    rsi_val = _round4(_safe_last(rsi_series))
    logger.debug(f"RSI({settings.RSI_PERIOD}): {rsi_val}")

    # ── MACD ──────────────────────────────────────────────────────────────────
    if _TA_AVAILABLE:
        macd_df = ta.macd(df["close"], fast=settings.MACD_FAST, slow=settings.MACD_SLOW, signal=settings.MACD_SIGNAL)
        macd_col = next((c for c in macd_df.columns if c.startswith("MACD_")), None)
        macdh_col = next((c for c in macd_df.columns if c.startswith("MACDh_")), None)
        macds_col = next((c for c in macd_df.columns if c.startswith("MACDs_")), None)
        macd_line_s = macd_df[macd_col] if macd_col else pd.Series(dtype=float)
        macd_hist_s = macd_df[macdh_col] if macdh_col else pd.Series(dtype=float)
        macd_sig_s  = macd_df[macds_col] if macds_col else pd.Series(dtype=float)
    else:
        macd_line_s, macd_sig_s, macd_hist_s = _macd_manual(df["close"], settings.MACD_FAST, settings.MACD_SLOW, settings.MACD_SIGNAL)

    macd_line = _round4(_safe_last(macd_line_s))
    macd_hist = _round4(_safe_last(macd_hist_s))
    macd_sig  = _round4(_safe_last(macd_sig_s))

    # Detect MACD cross: compare last two histogram values
    macd_cross: Literal["bullish", "bearish", "none"] = "none"
    hist_valid = macd_hist_s.dropna()
    if len(hist_valid) >= 2:
        prev_hist = float(hist_valid.iloc[-2])
        curr_hist = float(hist_valid.iloc[-1])
        if prev_hist < 0 and curr_hist >= 0:
            macd_cross = "bullish"
        elif prev_hist > 0 and curr_hist <= 0:
            macd_cross = "bearish"

    logger.debug(f"MACD: line={macd_line}, signal={macd_sig}, hist={macd_hist}, cross={macd_cross}")

    # ── Bollinger Bands ───────────────────────────────────────────────────────
    if _TA_AVAILABLE:
        bb_df = ta.bbands(df["close"], length=settings.BB_PERIOD, std=2.0)
        bbl_col = next((c for c in bb_df.columns if c.startswith("BBL_")), None)
        bbm_col = next((c for c in bb_df.columns if c.startswith("BBM_")), None)
        bbu_col = next((c for c in bb_df.columns if c.startswith("BBU_")), None)
        bbp_col = next((c for c in bb_df.columns if c.startswith("BBP_")), None)
        bb_lower_s = bb_df[bbl_col] if bbl_col else pd.Series(dtype=float)
        bb_mid_s   = bb_df[bbm_col] if bbm_col else pd.Series(dtype=float)
        bb_upper_s = bb_df[bbu_col] if bbu_col else pd.Series(dtype=float)
        bb_pct_s   = bb_df[bbp_col] if bbp_col else pd.Series(dtype=float)
    else:
        bb_lower_s, bb_mid_s, bb_upper_s, bb_pct_s = _bbands_manual(df["close"], settings.BB_PERIOD)

    bb_lower = _round4(_safe_last(bb_lower_s))
    bb_middle = _round4(_safe_last(bb_mid_s))
    bb_upper = _round4(_safe_last(bb_upper_s))
    bb_pct_b = _round4(_safe_last(bb_pct_s)) if not bb_pct_s.dropna().empty else 0.5

    logger.debug(f"BB: lower={bb_lower}, middle={bb_middle}, upper={bb_upper}, %B={bb_pct_b}")

    # ── VWAP ─────────────────────────────────────────────────────────────────
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    cumulative_pv = (typical_price * df["volume"]).cumsum()
    cumulative_vol = df["volume"].cumsum()
    vwap_series = cumulative_pv / cumulative_vol
    vwap_val = _round4(_safe_last(vwap_series))
    logger.debug(f"VWAP: {vwap_val}")

    # ── ATR ───────────────────────────────────────────────────────────────────
    if _TA_AVAILABLE:
        atr_series = ta.atr(df["high"], df["low"], df["close"], length=settings.ATR_PERIOD)
    else:
        atr_series = _atr_manual(df["high"], df["low"], df["close"], settings.ATR_PERIOD)
    atr_val = _round4(_safe_last(atr_series))
    logger.debug(f"ATR({settings.ATR_PERIOD}): {atr_val}")

    # ── EMA Fast / Slow ───────────────────────────────────────────────────────
    if _TA_AVAILABLE:
        ema_fast_series = ta.ema(df["close"], length=settings.EMA_FAST)
        ema_slow_series = ta.ema(df["close"], length=settings.EMA_SLOW)
    else:
        ema_fast_series = _ema(df["close"], settings.EMA_FAST)
        ema_slow_series = _ema(df["close"], settings.EMA_SLOW)

    ema_fast_val = _round4(_safe_last(ema_fast_series))
    ema_slow_val = _round4(_safe_last(ema_slow_series))

    ema_cross: Literal["bullish", "bearish", "none"] = "none"
    if len(ema_fast_series.dropna()) >= 2 and len(ema_slow_series.dropna()) >= 2:
        fast_prev = float(ema_fast_series.dropna().iloc[-2])
        fast_curr = float(ema_fast_series.dropna().iloc[-1])
        slow_prev = float(ema_slow_series.dropna().iloc[-2])
        slow_curr = float(ema_slow_series.dropna().iloc[-1])
        if fast_prev <= slow_prev and fast_curr > slow_curr:
            ema_cross = "bullish"
        elif fast_prev >= slow_prev and fast_curr < slow_curr:
            ema_cross = "bearish"

    logger.debug(f"EMA{settings.EMA_FAST}: {ema_fast_val}, EMA{settings.EMA_SLOW}: {ema_slow_val}, cross: {ema_cross}")

    # ── ADX ───────────────────────────────────────────────────────────────────
    if _TA_AVAILABLE:
        adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
        adx_col = next((c for c in adx_df.columns if c.startswith("ADX_")), None)
        adx_series = adx_df[adx_col] if adx_col else pd.Series(dtype=float)
    else:
        adx_series = _adx_manual(df["high"], df["low"], df["close"], 14)
    adx_val = _round4(_safe_last(adx_series))
    logger.debug(f"ADX: {adx_val}")

    # ── Current price and timestamp ───────────────────────────────────────────
    current_price = _round4(float(df["close"].iloc[-1]))
    timestamp_raw = df["timestamp"].iloc[-1] if "timestamp" in df.columns else pd.Timestamp.utcnow()
    if isinstance(timestamp_raw, pd.Timestamp):
        ts_str = timestamp_raw.isoformat()
    else:
        ts_str = str(timestamp_raw)

    return IndicatorSnapshot(
        rsi=rsi_val,
        macd_line=macd_line,
        macd_signal=macd_sig,
        macd_histogram=macd_hist,
        macd_cross_direction=macd_cross,
        bb_upper=bb_upper,
        bb_middle=bb_middle,
        bb_lower=bb_lower,
        bb_pct_b=bb_pct_b,
        vwap=vwap_val,
        atr=atr_val,
        ema_fast=ema_fast_val,
        ema_slow=ema_slow_val,
        ema_cross=ema_cross,
        adx=adx_val,
        current_price=current_price,
        timestamp=ts_str,
    )
