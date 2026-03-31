"""
Market regime detection — 5-state classifier.

Five regimes replace the original three to give the signal engine
the context it needs to apply the right strategy:

  TRENDING_UP_STRONG   ADX > 35 + 1h EMA slope positive
                       → Full trend-following mode. Momentum signals weighted heavily.
                       → Mean-reversion signals (RSI/BB) largely ignored.

  TRENDING_UP_WEAK     ADX 25–35 + 1h EMA slope positive
                       → Trend exists but lacks conviction. Use confluence gate strictly.
                       → Both momentum and some mean-reversion signals valid.

  RANGING              ADX ≤ 25
                       → Oscillation between support and resistance.
                       → Mean-reversion signals (RSI, BB, VWAP) dominate.
                       → Momentum/cross signals largely ignored.

  TRENDING_DOWN        ADX > 25 + 1h EMA slope negative
                       → Bear trend. Only sell/short signals acted on.
                       → Tight stops; avoid longs.

  VOLATILE             ATR z-score > 2.0 (abnormal volatility spike) OR
                       price range expansion > 3× average
                       → STAND ASIDE. No new positions.
                       → Confluence gate always fails in this regime.

Why VOLATILE matters: During flash crashes, liquidation cascades, and
major macro events, all indicator signals become unreliable. The model
simply cannot be trusted. Standing aside is the correct decision.

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
    Five-state market regime classification.

    Values:
        TRENDING_UP_STRONG:  ADX > 35, EMA slope positive. Full bull trend.
        TRENDING_UP_WEAK:    ADX 25–35, EMA slope positive. Weak/developing uptrend.
        RANGING:             ADX ≤ 25. No directional momentum.
        TRENDING_DOWN:       ADX > 25, EMA slope negative. Bear trend.
        VOLATILE:            ATR z-score > 2.0. Stand aside entirely.
    """

    TRENDING_UP_STRONG = "TRENDING_UP_STRONG"
    TRENDING_UP_WEAK   = "TRENDING_UP_WEAK"
    RANGING            = "RANGING"
    TRENDING_DOWN      = "TRENDING_DOWN"
    VOLATILE           = "VOLATILE"

    @property
    def is_bullish(self) -> bool:
        return self in (MarketRegime.TRENDING_UP_STRONG, MarketRegime.TRENDING_UP_WEAK)

    @property
    def is_bearish(self) -> bool:
        return self == MarketRegime.TRENDING_DOWN

    @property
    def is_trending(self) -> bool:
        return self in (
            MarketRegime.TRENDING_UP_STRONG,
            MarketRegime.TRENDING_UP_WEAK,
            MarketRegime.TRENDING_DOWN,
        )

    @property
    def stand_aside(self) -> bool:
        """True if the regime requires standing aside (no new trades)."""
        return self == MarketRegime.VOLATILE


class RegimeContext(BaseModel):
    """
    Result of multi-timeframe regime detection.

    Attributes:
        regime: Detected 5-state market regime.
        adx_value: ADX value from the 4h chart (trend strength).
        ema_slope: Slope of 21-period EMA on the 1h chart (direction).
        atr_z_score: Z-score of ATR — high values flag abnormal volatility.
        confidence: 0.0–1.0 confidence in the regime classification.
        description: Human-readable description of current conditions.

    Example:
        >>> ctx = detect_regime(df_1h, df_4h)
        >>> if ctx.regime.stand_aside:
        ...     logger.warning("VOLATILE regime — skipping cycle")
    """

    model_config = ConfigDict(frozen=True)

    regime: MarketRegime
    adx_value: float = Field(ge=0.0, le=100.0)
    ema_slope: float
    atr_z_score: float = Field(default=0.0, description="ATR z-score vs 50-candle history.")
    confidence: float = Field(ge=0.0, le=1.0)
    description: str


# ── Helper functions ──────────────────────────────────────────────────────────

def _compute_ema_slope(df: pd.DataFrame, period: int = 21, lookback: int = 5) -> float:
    """Compute fractional slope of an EMA over the last N candles."""
    if _TA_AVAILABLE:
        ema_series = ta.ema(df["close"], length=period)
    else:
        ema_series = df["close"].ewm(span=period, adjust=False).mean()
    valid = ema_series.dropna()

    if len(valid) < lookback + 1:
        return 0.0

    ema_now  = float(valid.iloc[-1])
    ema_past = float(valid.iloc[-(lookback + 1)])

    if ema_past == 0:
        return 0.0

    return round((ema_now - ema_past) / ema_past, 6)


def _compute_atr_zscore(df: pd.DataFrame, period: int = 14, z_window: int = 50) -> float:
    """Compute z-score of current ATR vs its rolling history."""
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr_series = tr.ewm(alpha=1 / period, adjust=False).mean().dropna()

    if len(atr_series) < 10:
        return 0.0

    tail = atr_series.tail(z_window)
    mean = float(tail.mean())
    std  = float(tail.std(ddof=1))
    if std < 1e-8:
        return 0.0

    return round((float(atr_series.iloc[-1]) - mean) / std, 3)


# ── ADX constants (loaded from settings; .env-configurable) ──────────────────
from backend.config.settings import settings as _settings  # noqa: E402
_ADX_STRONG     = _settings.adx_strong_threshold
_ADX_WEAK       = _settings.adx_weak_threshold
_ATR_Z_VOLATILE = _settings.atr_volatile_zscore


def detect_regime(df_1h: pd.DataFrame, df_4h: pd.DataFrame) -> RegimeContext:
    """
    Detect the current 5-state market regime.

    Evaluation order:
    1. VOLATILE check first (ATR z-score > 2.0 on 1h) — stand aside
    2. Trending vs Ranging via 4h ADX
    3. Trend strength (STRONG vs WEAK) via ADX magnitude
    4. Trend direction via 1h EMA slope

    Args:
        df_1h: OHLCV DataFrame at 1-hour candles. Needs ≥ 30 rows.
        df_4h: OHLCV DataFrame at 4-hour candles. Needs ≥ 20 rows.

    Returns:
        RegimeContext with 5-state regime, ADX, EMA slope, ATR z-score.

    Raises:
        ValueError: If either DataFrame is too short.

    Example:
        >>> ctx = detect_regime(df_1h, df_4h)
        >>> print(ctx.regime.value, ctx.adx_value)
    """
    if len(df_4h) < 20:
        raise ValueError(f"4h DataFrame too short: {len(df_4h)} rows (need ≥ 20)")
    if len(df_1h) < 20:
        raise ValueError(f"1h DataFrame too short: {len(df_1h)} rows (need ≥ 20)")

    # ── Step 1: Volatility check (VOLATILE regime) ─────────────────────────
    atr_z = _compute_atr_zscore(df_1h)
    if atr_z >= _ATR_Z_VOLATILE:
        logger.warning(
            f"VOLATILE regime detected: ATR z-score={atr_z:.2f} ≥ {_ATR_Z_VOLATILE} — "
            f"standing aside, no new trades."
        )
        return RegimeContext(
            regime=MarketRegime.VOLATILE,
            adx_value=0.0,
            ema_slope=0.0,
            atr_z_score=atr_z,
            confidence=min(1.0, round((atr_z - _ATR_Z_VOLATILE) / 2.0, 4)),
            description=(
                f"VOLATILE: ATR z-score={atr_z:.2f} (>{_ATR_Z_VOLATILE}) — "
                f"abnormal price movement detected. Stand aside."
            ),
        )

    # ── Step 2: 4h ADX for trend strength ──────────────────────────────────
    adx_val = 0.0
    if _TA_AVAILABLE:
        adx_df = ta.adx(df_4h["high"], df_4h["low"], df_4h["close"], length=14)
        adx_col = next((c for c in adx_df.columns if c.startswith("ADX_")), None)
        if adx_col:
            valid_adx = adx_df[adx_col].dropna()
            if not valid_adx.empty:
                adx_val = round(float(valid_adx.iloc[-1]), 4)
    else:
        from backend.indicators.engine import _adx_manual
        adx_series = _adx_manual(df_4h["high"], df_4h["low"], df_4h["close"], 14)
        valid_adx  = adx_series.dropna()
        if not valid_adx.empty:
            adx_val = round(float(valid_adx.iloc[-1]), 4)

    # ── Step 3: 1h EMA slope for direction ─────────────────────────────────
    ema_slope = _compute_ema_slope(df_1h, period=21, lookback=5)

    # ── Step 4: Classify regime ─────────────────────────────────────────────
    is_trending = adx_val > _ADX_WEAK
    is_strong   = adx_val > _ADX_STRONG
    is_up       = ema_slope > 0

    if not is_trending:
        regime = MarketRegime.RANGING
        description = (
            f"Ranging: ADX={adx_val:.1f} (≤{_ADX_WEAK}) — no directional trend. "
            f"Mean-reversion signals apply."
        )
        confidence = min(1.0, round((_ADX_WEAK - adx_val) / _ADX_WEAK, 4))

    elif is_up:
        if is_strong:
            regime = MarketRegime.TRENDING_UP_STRONG
            description = (
                f"Strong uptrend: ADX={adx_val:.1f} (>{_ADX_STRONG}), "
                f"1h EMA slope={ema_slope:+.4f}. Momentum signals dominate."
            )
        else:
            regime = MarketRegime.TRENDING_UP_WEAK
            description = (
                f"Weak uptrend: ADX={adx_val:.1f} ({_ADX_WEAK}–{_ADX_STRONG}), "
                f"1h EMA slope={ema_slope:+.4f}. Use strict confluence gate."
            )
        confidence = min(1.0, round((adx_val - _ADX_WEAK) / _ADX_WEAK, 4))

    else:
        regime = MarketRegime.TRENDING_DOWN
        description = (
            f"Downtrend: ADX={adx_val:.1f} (>{_ADX_WEAK}), "
            f"1h EMA slope={ema_slope:+.4f}. Only sell signals valid."
        )
        confidence = min(1.0, round((adx_val - _ADX_WEAK) / _ADX_WEAK, 4))

    logger.info(
        f"Regime: {regime.value} | ADX={adx_val:.1f} | "
        f"EMA slope={ema_slope:+.4f} | ATR_z={atr_z:.2f} | conf={confidence:.2f}"
    )

    return RegimeContext(
        regime=regime,
        adx_value=adx_val,
        ema_slope=ema_slope,
        atr_z_score=atr_z,
        confidence=confidence,
        description=description,
    )
