"""
Market regime detection — classifies the current market structure.

Determines whether the market is in a trending or ranging state using
multi-timeframe analysis:

- **4h timeframe ADX**: ADX > 25 indicates a trending regime. This is a widely
  accepted threshold from J. Welles Wilder's original ADX definition.
- **1h timeframe EMA slope**: The direction of the 21-period EMA slope on the
  1h chart determines whether the trend is UP or DOWN.

Why multi-timeframe? The 4h provides the macro context (is there a trend at all?)
while the 1h provides the directional bias (which way is it trending?). This
prevents trading against the larger trend on noise from shorter timeframes.

MarketRegime values:
- TRENDING_UP: ADX > 25 AND 1h EMA slope positive
- TRENDING_DOWN: ADX > 25 AND 1h EMA slope negative
- RANGING: ADX ≤ 25 (no dominant trend)

Role in system: The regime context modifies the rule engine's behaviour and
is included in the Gemini prompt so the LLM can adjust its strategy accordingly.
In ranging markets, mean-reversion rules are preferred; in trending markets,
momentum/crossover rules apply.

Dependencies: pandas, pandas_ta, pydantic, loguru
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

import numpy as np
import pandas as pd
from loguru import logger

try:
    import pandas_ta as ta
    _TA_AVAILABLE = True
except ImportError:
    ta = None  # type: ignore[assignment]
    _TA_AVAILABLE = False
from pydantic import BaseModel, ConfigDict, Field


class MarketRegime(str, Enum):
    """
    Enumeration of possible market regime states.

    Values:
        TRENDING_UP: ADX > 25 and price is advancing.
        TRENDING_DOWN: ADX > 25 and price is declining.
        RANGING: ADX ≤ 25 — market lacks directional momentum.
    """

    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"


class RegimeContext(BaseModel):
    """
    Result of multi-timeframe regime detection.

    Attributes:
        regime: Detected market regime enum value.
        adx_value: ADX value from the 4h chart (trend strength).
        ema_slope: Slope of the 21-period EMA on the 1h chart (direction).
        confidence: 0.0–1.0 confidence in the regime classification.
        description: Human-readable description of current market conditions.

    Example:
        >>> ctx = detect_regime(df_1h, df_4h)
        >>> print(ctx.regime.value, ctx.adx_value)
    """

    model_config = ConfigDict(frozen=True)

    regime: MarketRegime
    adx_value: float = Field(ge=0.0, le=100.0)
    ema_slope: float
    confidence: float = Field(ge=0.0, le=1.0)
    description: str


def _compute_ema_slope(df: pd.DataFrame, period: int = 21, lookback: int = 5) -> float:
    """
    Compute the slope of an EMA over the last N candles.

    Args:
        df: OHLCV DataFrame with 'close' column.
        period: EMA period to compute. Defaults to 21.
        lookback: Number of candles over which to measure slope. Defaults to 5.

    Returns:
        Fractional slope. Returns 0.0 if insufficient data.

    Example:
        >>> slope = _compute_ema_slope(df_1h)
    """
    if _TA_AVAILABLE:
        ema_series = ta.ema(df["close"], length=period)
    else:
        ema_series = df["close"].ewm(span=period, adjust=False).mean()
    valid = ema_series.dropna()

    if len(valid) < lookback + 1:
        return 0.0

    ema_now = float(valid.iloc[-1])
    ema_past = float(valid.iloc[-(lookback + 1)])

    if ema_past == 0:
        return 0.0

    return round((ema_now - ema_past) / ema_past, 6)


def detect_regime(df_1h: pd.DataFrame, df_4h: pd.DataFrame) -> RegimeContext:
    """
    Detect the current market regime using multi-timeframe ADX and EMA slope.

    The 4h ADX determines whether the market is trending at all.
    The 1h EMA slope determines the direction of the trend.
    Confidence is derived from how strongly ADX deviates from the threshold.

    Args:
        df_1h: OHLCV DataFrame at 1-hour candles. Needs at least 30 rows.
        df_4h: OHLCV DataFrame at 4-hour candles. Needs at least 30 rows.

    Returns:
        RegimeContext with the detected regime, raw ADX, EMA slope, and confidence.

    Raises:
        ValueError: If either DataFrame has fewer than 20 rows.

    Example:
        >>> regime = detect_regime(df_1h, df_4h)
        >>> if regime.regime == MarketRegime.TRENDING_UP:
        ...     print("Follow the trend")
    """
    if len(df_4h) < 20:
        raise ValueError(f"4h DataFrame too short: {len(df_4h)} rows (need ≥ 20)")
    if len(df_1h) < 20:
        raise ValueError(f"1h DataFrame too short: {len(df_1h)} rows (need ≥ 20)")

    # ── 4h ADX for trend strength ─────────────────────────────────────────────
    adx_val = 0.0
    if _TA_AVAILABLE:
        adx_df_4h = ta.adx(df_4h["high"], df_4h["low"], df_4h["close"], length=14)
        adx_col = next((c for c in adx_df_4h.columns if c.startswith("ADX_")), None)
        if adx_col:
            valid_adx = adx_df_4h[adx_col].dropna()
            if not valid_adx.empty:
                adx_val = round(float(valid_adx.iloc[-1]), 4)
    else:
        # Manual ADX via DX smoothing
        from backend.indicators.engine import _adx_manual
        adx_series = _adx_manual(df_4h["high"], df_4h["low"], df_4h["close"], 14)
        valid_adx = adx_series.dropna()
        if not valid_adx.empty:
            adx_val = round(float(valid_adx.iloc[-1]), 4)

    # ── 1h EMA slope for direction ────────────────────────────────────────────
    ema_slope = _compute_ema_slope(df_1h, period=21, lookback=5)

    # ── Regime classification ─────────────────────────────────────────────────
    _ADX_THRESHOLD = 25.0
    is_trending = adx_val > _ADX_THRESHOLD

    if is_trending:
        if ema_slope > 0:
            regime = MarketRegime.TRENDING_UP
            description = (
                f"Strong uptrend: ADX={adx_val:.1f} (>{_ADX_THRESHOLD}), "
                f"1h EMA slope={ema_slope:+.4f} (positive)"
            )
        else:
            regime = MarketRegime.TRENDING_DOWN
            description = (
                f"Strong downtrend: ADX={adx_val:.1f} (>{_ADX_THRESHOLD}), "
                f"1h EMA slope={ema_slope:+.4f} (negative)"
            )
    else:
        regime = MarketRegime.RANGING
        description = (
            f"Ranging/consolidating: ADX={adx_val:.1f} (≤{_ADX_THRESHOLD}), "
            f"no clear trend direction"
        )

    # Confidence: how far ADX is from the threshold, normalised 0–1
    # Strong trend (ADX=50) → confidence 1.0; weak/borderline (ADX=25) → confidence 0.0
    if is_trending:
        confidence = min(1.0, round((adx_val - _ADX_THRESHOLD) / _ADX_THRESHOLD, 4))
    else:
        # In ranging: confidence in the "no trend" assessment
        confidence = min(1.0, round((_ADX_THRESHOLD - adx_val) / _ADX_THRESHOLD, 4))

    logger.info(f"Market regime: {regime.value} | ADX={adx_val:.1f} | EMA slope={ema_slope:+.4f} | confidence={confidence:.2f}")

    return RegimeContext(
        regime=regime,
        adx_value=adx_val,
        ema_slope=ema_slope,
        confidence=confidence,
        description=description,
    )
