"""
Confluence scoring engine — aggregates indicator signals into a unified score.

The confluence score answers a simple question: "How many independent signals
agree on a directional bias right now?" When 5 or more out of 8 signals agree,
the threshold is passed and the agent proceeds to AI analysis and potential
trade execution.

This design filters out low-confidence situations where only 1–2 indicators
trigger, which typically results in false signals or choppy-market noise.

Scoring methodology (each criterion contributes 1 point to either bull or bear):
 1. RSI oversold (<35) → +1 bull | RSI overbought (>65) → +1 bear
 2. MACD bullish cross → +1 bull | MACD bearish cross → +1 bear
 3. Price at/below BB lower → +1 bull | at/above BB upper → +1 bear
 4. Price above VWAP → +1 bull | below VWAP → +1 bear
 5. EMA9 > EMA21 → +1 bull | EMA9 < EMA21 → +1 bear
 6. Fear & Greed < 25 (extreme fear) → +1 bull | > 75 → +1 bear
 7. Positive news sentiment → +1 bull | negative → +1 bear
 8. ADX > 25 (trending) → +1 for the dominant EMA direction

Maximum possible score: 8 bull OR 8 bear.
Threshold to pass: dominant direction count >= 5.

Role in system: Gate between raw indicator data and AI decision layer.
If confluence.passes_threshold is False, the cycle is skipped entirely.

Dependencies: pydantic, loguru
"""

from __future__ import annotations

from typing import Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from backend.data.sentiment import SentimentSnapshot
from backend.indicators.engine import IndicatorSnapshot

# Confluence threshold — must be tuned carefully to balance sensitivity/specificity
# Production: 5 (high conviction, fewer false signals)
# Testing/validation: 3 (passes current bearish market conditions for pipeline validation)
_CONFLUENCE_THRESHOLD = 5


class ConfluentSignal(BaseModel):
    """
    Result of the confluence scoring process for one trading cycle.

    Attributes:
        bullish_count: Number of signals pointing bullish (0–8).
        bearish_count: Number of signals pointing bearish (0–8).
        dominant_direction: The direction with more signal support.
        score: The dominant direction's count (same as max(bullish, bearish)).
        passes_threshold: True if score >= 5, meaning the agent should proceed.
        signal_breakdown: Human-readable description of each signal's contribution.

    Example:
        >>> signal = score(snapshot, sentiment)
        >>> if signal.passes_threshold:
        ...     print(f"Trade signal: {signal.dominant_direction}")
    """

    model_config = ConfigDict(frozen=True)

    bullish_count: int = Field(ge=0, le=8)
    bearish_count: int = Field(ge=0, le=8)
    dominant_direction: Literal["bullish", "bearish", "neutral"]
    score: int = Field(ge=0, le=8)
    passes_threshold: bool
    signal_breakdown: list[str] = Field(default_factory=list)


def score(snapshot: IndicatorSnapshot, sentiment: SentimentSnapshot) -> ConfluentSignal:
    """
    Compute the confluence score from indicator and sentiment data.

    Evaluates 8 independent boolean criteria — each one votes for either
    the bull or bear camp. The dominant camp wins. A score >= 5 is required
    to pass the confluence gate and proceed to AI analysis.

    Args:
        snapshot: Computed indicator values from engine.compute_indicators().
        sentiment: Market sentiment snapshot from sentiment.get_sentiment_snapshot().

    Returns:
        ConfluentSignal with the vote tally, dominant direction, and threshold result.

    Example:
        >>> signal = score(indicator_snap, sentiment_snap)
        >>> print(f"Bull: {signal.bullish_count}, Bear: {signal.bearish_count}")
    """
    bull = 0
    bear = 0
    breakdown: list[str] = []

    # ── Signal 1: RSI momentum ────────────────────────────────────────────────
    # RSI < 35: oversold — mean reversion opportunity (more conservative than standard 30)
    # RSI > 65: overbought — potential reversal or short signal
    if snapshot.rsi < 35:
        bull += 1
        breakdown.append(f"RSI={snapshot.rsi:.1f} (<35) → BULL")
    elif snapshot.rsi > 65:
        bear += 1
        breakdown.append(f"RSI={snapshot.rsi:.1f} (>65) → BEAR")
    else:
        breakdown.append(f"RSI={snapshot.rsi:.1f} (neutral)")

    # ── Signal 2: MACD cross ──────────────────────────────────────────────────
    # A fresh histogram sign change (cross) is more reliable than absolute MACD value
    if snapshot.macd_cross_direction == "bullish":
        bull += 1
        breakdown.append(f"MACD cross=BULLISH → BULL")
    elif snapshot.macd_cross_direction == "bearish":
        bear += 1
        breakdown.append(f"MACD cross=BEARISH → BEAR")
    else:
        # No fresh cross — use histogram sign for weaker signal
        if snapshot.macd_histogram > 0:
            breakdown.append(f"MACD hist={snapshot.macd_histogram:.4f} (no cross, positive, neutral)")
        else:
            breakdown.append(f"MACD hist={snapshot.macd_histogram:.4f} (no cross, negative, neutral)")

    # ── Signal 3: Bollinger Bands position ────────────────────────────────────
    # %B < 0.05: price touching or below lower band (oversold within volatility envelope)
    # %B > 0.95: price touching or above upper band (overbought)
    if snapshot.bb_pct_b <= 0.05:
        bull += 1
        breakdown.append(f"BB %B={snapshot.bb_pct_b:.3f} (≤0.05, at lower band) → BULL")
    elif snapshot.bb_pct_b >= 0.95:
        bear += 1
        breakdown.append(f"BB %B={snapshot.bb_pct_b:.3f} (≥0.95, at upper band) → BEAR")
    else:
        breakdown.append(f"BB %B={snapshot.bb_pct_b:.3f} (middle zone, neutral)")

    # ── Signal 4: VWAP relationship ───────────────────────────────────────────
    # Price above VWAP: buyers are in control (institutional bias)
    # Price below VWAP: sellers dominate
    if snapshot.current_price > snapshot.vwap and snapshot.vwap > 0:
        bull += 1
        breakdown.append(f"Price={snapshot.current_price:.2f} > VWAP={snapshot.vwap:.2f} → BULL")
    elif snapshot.current_price < snapshot.vwap and snapshot.vwap > 0:
        bear += 1
        breakdown.append(f"Price={snapshot.current_price:.2f} < VWAP={snapshot.vwap:.2f} → BEAR")
    else:
        breakdown.append(f"Price≈VWAP (neutral)")

    # ── Signal 5: EMA crossover state ─────────────────────────────────────────
    # EMA9 > EMA21: short-term momentum is bullish
    # EMA9 < EMA21: short-term momentum is bearish
    if snapshot.ema_fast > snapshot.ema_slow:
        bull += 1
        breakdown.append(f"EMA{9}={snapshot.ema_fast:.2f} > EMA{21}={snapshot.ema_slow:.2f} → BULL")
    elif snapshot.ema_fast < snapshot.ema_slow:
        bear += 1
        breakdown.append(f"EMA{9}={snapshot.ema_fast:.2f} < EMA{21}={snapshot.ema_slow:.2f} → BEAR")
    else:
        breakdown.append("EMA9≈EMA21 (neutral)")

    # ── Signal 6: Fear & Greed Index ─────────────────────────────────────────
    # Contrarian signal: extreme fear is often a buying opportunity
    fgi = sentiment.fear_greed_value
    if fgi < 25:
        bull += 1
        breakdown.append(f"Fear&Greed={fgi} (<25, extreme fear) → BULL (contrarian)")
    elif fgi > 75:
        bear += 1
        breakdown.append(f"Fear&Greed={fgi} (>75, extreme greed) → BEAR (contrarian)")
    else:
        breakdown.append(f"Fear&Greed={fgi} (neutral zone)")

    # ── Signal 7: News sentiment ──────────────────────────────────────────────
    news_sent = sentiment.overall_news_sentiment
    if news_sent == "positive":
        bull += 1
        breakdown.append(f"News sentiment=POSITIVE → BULL")
    elif news_sent == "negative":
        bear += 1
        breakdown.append(f"News sentiment=NEGATIVE → BEAR")
    else:
        breakdown.append(f"News sentiment=NEUTRAL")

    # ── Signal 8: ADX trend strength → dominant direction ─────────────────────
    # ADX > 25: market is trending. Give +1 to whichever EMA direction is dominant.
    # This rewards trend-following when the trend is confirmed.
    if snapshot.adx > 25:
        if snapshot.ema_fast > snapshot.ema_slow:
            bull += 1
            breakdown.append(f"ADX={snapshot.adx:.1f} (>25, trending UP) → BULL")
        else:
            bear += 1
            breakdown.append(f"ADX={snapshot.adx:.1f} (>25, trending DOWN) → BEAR")
    else:
        breakdown.append(f"ADX={snapshot.adx:.1f} (≤25, ranging, no trend bonus)")

    # ── Aggregate ─────────────────────────────────────────────────────────────
    if bull > bear:
        dominant: Literal["bullish", "bearish", "neutral"] = "bullish"
        score_val = bull
    elif bear > bull:
        dominant = "bearish"
        score_val = bear
    else:
        dominant = "neutral"
        score_val = bull  # Tied — use bull count (doesn't matter, threshold won't pass)

    passes = score_val >= _CONFLUENCE_THRESHOLD

    logger.info(
        f"Confluence: bull={bull}, bear={bear}, dominant={dominant}, "
        f"score={score_val}/8, passes={passes}"
    )

    return ConfluentSignal(
        bullish_count=bull,
        bearish_count=bear,
        dominant_direction=dominant,
        score=score_val,
        passes_threshold=passes,
        signal_breakdown=breakdown,
    )
