"""
Confluence scoring engine — regime-conditional, signal-quality-weighted.

Architecture improvements over the original:

1. REGIME-CONDITIONAL SIGNAL STACKS
   Same indicator means different things in different regimes.
   In TRENDING regimes: momentum signals (EMA cross, MACD, ADX) are weighted heavily.
   In RANGING regime:   mean-reversion signals (RSI, BB, VWAP) dominate.
   In VOLATILE regime:  gate immediately fails — stand aside.

2. SIGNAL QUALITY SCORING (z-score weighting)
   Each signal is weighted by how extreme it is relative to recent history.
   RSI z-score of -2.5 (deeply unusual oversold) → high quality signal.
   RSI z-score of -0.3 (mildly oversold, common) → low quality signal.
   Quality weight = min(1.0, abs(z_score) / 2.0) → 0.0 at z=0, 1.0 at z=2.

3. EXTENDED SIGNALS (10 total, up from 8)
   Signals 9 and 10 are the new microstructure inputs.

   Signal 9:  Funding Rate — extreme positive funding (>0.05%) → bearish;
              extreme negative funding (<-0.05%) → bullish (shorts squeezable).
   Signal 10: Volume Profile — price below Value Area Low → bullish;
              price above Value Area High → bearish.

4. ADAPTIVE CONFLUENCE THRESHOLD
   Normal conditions:    settings.confluence_min_score (default 4/10)
   High tail risk:       settings.confluence_volatile_score (default 6/10) when
                         narrative.require_higher_confluence is True.
   VOLATILE regime:      gate immediately fails.

Scoring methodology:
  Each of 10 signals contributes a quality-weighted vote to bull or bear camp.
  Quality weight ∈ [0.5, 1.0]: baseline 0.5 for any signal, +0.5 if |z| ≥ 2.
  Dominant direction score = sum(quality_weight × signal_contribution).
  passes_threshold = dominant_score ≥ adaptive_threshold.

Dependencies: pydantic, loguru, indicators.engine, indicators.regime
"""

from __future__ import annotations

from typing import Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from backend.config.settings import settings as _settings
from backend.data.market_data import FundingData, MicrostructureSnapshot
from backend.data.sentiment import SentimentSnapshot
from backend.indicators.engine import IndicatorSnapshot
from backend.indicators.regime import MarketRegime
from backend.indicators.volume_profile import VolumeProfileResult


class ConfluentSignal(BaseModel):
    """
    Result of the regime-conditional, quality-weighted confluence scoring.

    Attributes:
        bullish_count: Raw signal vote count for bullish direction (0–10).
        bearish_count: Raw signal vote count for bearish direction (0–10).
        bullish_score: Quality-weighted bullish score (0.0–10.0).
        bearish_score: Quality-weighted bearish score (0.0–10.0).
        dominant_direction: Direction with higher weighted score.
        score: Raw dominant vote count (for UI display).
        weighted_score: Quality-weighted dominant score (used for threshold check).
        passes_threshold: True if weighted_score ≥ adaptive_threshold.
        adaptive_threshold: The threshold actually used this cycle.
        signal_breakdown: Human-readable list of each signal's contribution.
        regime_applied: The regime used for signal weighting.

    Example:
        >>> sig = score(snapshot, sentiment, regime, microstructure, volume_profile)
        >>> if sig.passes_threshold:
        ...     print(f"{sig.dominant_direction} with score {sig.weighted_score:.1f}")
    """

    model_config = ConfigDict(frozen=True)

    bullish_count: int = Field(ge=0, le=10)
    bearish_count: int = Field(ge=0, le=10)
    bullish_score: float = Field(ge=0.0)
    bearish_score: float = Field(ge=0.0)
    dominant_direction: Literal["bullish", "bearish", "neutral"]
    score: int = Field(ge=0, le=10)
    weighted_score: float = Field(ge=0.0)
    passes_threshold: bool
    adaptive_threshold: float
    signal_breakdown: list[str] = Field(default_factory=list)
    regime_applied: str


# ── Signal quality weight from z-score ───────────────────────────────────────

def _quality(z: float) -> float:
    """
    Map a z-score to a signal quality weight in [0.5, 1.0].

    |z| = 0   → 0.5 (weak signal, still counts but at half weight)
    |z| = 1   → 0.75
    |z| ≥ 2   → 1.0 (extreme signal, full weight)
    """
    return min(1.0, 0.5 + abs(z) / 4.0)


# ── Regime weight tables ──────────────────────────────────────────────────────
# Multiplier applied to each signal group based on current regime.
# 1.0 = normal weight, 0.3 = largely ignored, 1.5 = extra weight.

_REGIME_WEIGHTS: dict[MarketRegime, dict[str, float]] = {
    MarketRegime.TRENDING_UP_STRONG: {
        "rsi":       0.3,   # Oversold RSI in a strong uptrend = normal pullback, not reversal
        "macd":      1.5,
        "bb":        0.3,   # Price above BB upper is fine in strong trends
        "vwap":      1.0,
        "ema":       1.5,
        "sentiment": 0.8,
        "adx":       1.5,
        "funding":   1.0,
        "vp":        1.0,
        "delta":     1.2,
    },
    MarketRegime.TRENDING_UP_WEAK: {
        "rsi":       0.7,
        "macd":      1.2,
        "bb":        0.7,
        "vwap":      1.0,
        "ema":       1.2,
        "sentiment": 0.8,
        "adx":       1.0,
        "funding":   1.0,
        "vp":        1.0,
        "delta":     1.0,
    },
    MarketRegime.RANGING: {
        "rsi":       1.5,   # Mean reversion king in ranging markets
        "macd":      0.5,   # MACD cross in ranging = noise
        "bb":        1.5,   # BB bands are primary S/R in ranging
        "vwap":      1.3,
        "ema":       0.3,   # EMA cross in ranging = whipsaw
        "sentiment": 0.8,
        "adx":       0.3,   # ADX is low in ranging by definition
        "funding":   1.0,
        "vp":        1.5,   # Volume Profile POC is excellent in ranging
        "delta":     1.0,
    },
    MarketRegime.TRENDING_DOWN: {
        "rsi":       0.3,
        "macd":      1.5,
        "bb":        0.3,
        "vwap":      1.0,
        "ema":       1.5,
        "sentiment": 0.8,
        "adx":       1.5,
        "funding":   1.0,
        "vp":        1.0,
        "delta":     1.2,
    },
    MarketRegime.VOLATILE: {  # Stand aside — weights irrelevant
        "rsi":       0.0,
        "macd":      0.0,
        "bb":        0.0,
        "vwap":      0.0,
        "ema":       0.0,
        "sentiment": 0.0,
        "adx":       0.0,
        "funding":   0.0,
        "vp":        0.0,
        "delta":     0.0,
    },
}


def score(
    snapshot: IndicatorSnapshot,
    sentiment: SentimentSnapshot,
    regime: MarketRegime = MarketRegime.RANGING,
    microstructure: MicrostructureSnapshot | None = None,
    volume_profile: VolumeProfileResult | None = None,
    require_higher_confluence: bool = False,
) -> ConfluentSignal:
    """
    Compute the regime-conditional, quality-weighted confluence score.

    Evaluates 10 independent signals. Each signal is:
      1. Weighted by the current regime (momentum vs mean-reversion)
      2. Quality-weighted by z-score magnitude (extreme = higher quality)

    VOLATILE regime immediately returns passes_threshold=False.

    Args:
        snapshot: Computed indicator values from engine.compute_indicators().
        sentiment: Market sentiment snapshot.
        regime: Current market regime from detect_regime().
        microstructure: Optional funding + L/S ratio data.
        volume_profile: Optional volume profile computation.
        require_higher_confluence: If True (from NarrativeContext), use
                                   settings.confluence_volatile_score threshold.

    Returns:
        ConfluentSignal with weighted score, threshold result, and full breakdown.

    Example:
        >>> sig = score(ind, sent, regime=MarketRegime.RANGING, microstructure=ms, vp=vp)
        >>> print(f"Score: {sig.weighted_score:.1f}/{sig.adaptive_threshold:.1f}")
    """

    # ── VOLATILE: immediate stand-aside ───────────────────────────────────────
    if regime == MarketRegime.VOLATILE:
        logger.warning("Confluence gate: VOLATILE regime — automatic fail, standing aside.")
        return ConfluentSignal(
            bullish_count=0, bearish_count=0,
            bullish_score=0.0, bearish_score=0.0,
            dominant_direction="neutral",
            score=0, weighted_score=0.0,
            passes_threshold=False,
            adaptive_threshold=float(_settings.confluence_volatile_score),
            signal_breakdown=["VOLATILE regime — all signals suppressed, stand aside."],
            regime_applied=regime.value,
        )

    weights = _REGIME_WEIGHTS.get(regime, _REGIME_WEIGHTS[MarketRegime.RANGING])

    bull_raw   = 0
    bear_raw   = 0
    bull_score = 0.0
    bear_score = 0.0
    breakdown: list[str] = []

    def _add(direction: str, group: str, z: float, label: str) -> None:
        nonlocal bull_raw, bear_raw, bull_score, bear_score
        q = _quality(z) * weights.get(group, 1.0)
        if direction == "bull":
            bull_raw   += 1
            bull_score += q
            breakdown.append(f"[BULL] {label} (q={q:.2f})")
        else:
            bear_raw   += 1
            bear_score += q
            breakdown.append(f"[BEAR] {label} (q={q:.2f})")

    # ── Signal 1: RSI ─────────────────────────────────────────────────────────
    z_rsi = snapshot.z_rsi
    if snapshot.rsi < 35:
        _add("bull", "rsi", z_rsi, f"RSI={snapshot.rsi:.1f} (<35, oversold) z={z_rsi:.2f}")
    elif snapshot.rsi > 65:
        _add("bear", "rsi", z_rsi, f"RSI={snapshot.rsi:.1f} (>65, overbought) z={z_rsi:.2f}")
    else:
        breakdown.append(f"RSI={snapshot.rsi:.1f} (neutral)")

    # ── Signal 2: MACD ────────────────────────────────────────────────────────
    if snapshot.macd_cross_direction == "bullish":
        _add("bull", "macd", abs(snapshot.macd_histogram) / max(abs(snapshot.macd_line), 0.001),
             f"MACD cross=BULLISH hist={snapshot.macd_histogram:.4f}")
    elif snapshot.macd_cross_direction == "bearish":
        _add("bear", "macd", abs(snapshot.macd_histogram) / max(abs(snapshot.macd_line), 0.001),
             f"MACD cross=BEARISH hist={snapshot.macd_histogram:.4f}")
    else:
        breakdown.append(
            f"MACD hist={snapshot.macd_histogram:.4f} (no cross, "
            f"{'positive' if snapshot.macd_histogram > 0 else 'negative'})"
        )

    # ── Signal 3: Bollinger Bands ─────────────────────────────────────────────
    if snapshot.bb_pct_b <= 0.05:
        _add("bull", "bb", max(0.05 - snapshot.bb_pct_b, 0) * 20,
             f"BB %B={snapshot.bb_pct_b:.3f} (at lower band)")
    elif snapshot.bb_pct_b >= 0.95:
        _add("bear", "bb", max(snapshot.bb_pct_b - 0.95, 0) * 20,
             f"BB %B={snapshot.bb_pct_b:.3f} (at upper band)")
    else:
        breakdown.append(f"BB %B={snapshot.bb_pct_b:.3f} (mid-band, neutral)")

    # ── Signal 4: VWAP Standard Deviation Bands ──────────────────────────────
    # Use VWAP SD bands instead of simple above/below VWAP
    if snapshot.vwap_lower_2sd > 0 and snapshot.current_price <= snapshot.vwap_lower_2sd:
        _add("bull", "vwap", 2.0, f"Price at VWAP −2σ=${snapshot.vwap_lower_2sd:,.2f} (deep discount)")
    elif snapshot.vwap_lower_1sd > 0 and snapshot.current_price <= snapshot.vwap_lower_1sd:
        _add("bull", "vwap", 1.0, f"Price at VWAP −1σ=${snapshot.vwap_lower_1sd:,.2f}")
    elif snapshot.vwap_upper_2sd > 0 and snapshot.current_price >= snapshot.vwap_upper_2sd:
        _add("bear", "vwap", 2.0, f"Price at VWAP +2σ=${snapshot.vwap_upper_2sd:,.2f} (deep premium)")
    elif snapshot.vwap_upper_1sd > 0 and snapshot.current_price >= snapshot.vwap_upper_1sd:
        _add("bear", "vwap", 1.0, f"Price at VWAP +1σ=${snapshot.vwap_upper_1sd:,.2f}")
    elif snapshot.vwap > 0:
        # Fallback to simple above/below
        if snapshot.current_price > snapshot.vwap:
            _add("bull", "vwap", 0.5, f"Price ${snapshot.current_price:,.2f} > VWAP ${snapshot.vwap:,.2f}")
        elif snapshot.current_price < snapshot.vwap:
            _add("bear", "vwap", 0.5, f"Price ${snapshot.current_price:,.2f} < VWAP ${snapshot.vwap:,.2f}")
        else:
            breakdown.append("Price ≈ VWAP (neutral)")
    else:
        breakdown.append("VWAP unavailable")

    # ── Signal 5: EMA cross state ─────────────────────────────────────────────
    if snapshot.ema_fast > snapshot.ema_slow:
        _add("bull", "ema", abs(snapshot.ema_fast - snapshot.ema_slow) / snapshot.ema_slow,
             f"EMA9={snapshot.ema_fast:.2f} > EMA21={snapshot.ema_slow:.2f}")
    elif snapshot.ema_fast < snapshot.ema_slow:
        _add("bear", "ema", abs(snapshot.ema_fast - snapshot.ema_slow) / snapshot.ema_slow,
             f"EMA9={snapshot.ema_fast:.2f} < EMA21={snapshot.ema_slow:.2f}")
    else:
        breakdown.append("EMA9 ≈ EMA21 (neutral)")

    # ── Signal 6: Fear & Greed ────────────────────────────────────────────────
    fgi = sentiment.fear_greed_value
    if fgi < 25:
        _add("bull", "sentiment", (25 - fgi) / 25, f"Fear&Greed={fgi} (<25, extreme fear)")
    elif fgi > 75:
        _add("bear", "sentiment", (fgi - 75) / 25, f"Fear&Greed={fgi} (>75, extreme greed)")
    else:
        breakdown.append(f"Fear&Greed={fgi} (neutral)")

    # ── Signal 7: News sentiment ──────────────────────────────────────────────
    ns = sentiment.overall_news_sentiment
    if ns == "positive":
        _add("bull", "sentiment", 0.5, "News=POSITIVE")
    elif ns == "negative":
        _add("bear", "sentiment", 0.5, "News=NEGATIVE")
    else:
        breakdown.append("News=NEUTRAL")

    # ── Signal 8: ADX trend confirmation ─────────────────────────────────────
    if snapshot.adx > 25:
        z_adx = (snapshot.adx - 25) / 10.0  # proxy z-score
        if snapshot.ema_fast > snapshot.ema_slow:
            _add("bull", "adx", z_adx, f"ADX={snapshot.adx:.1f} (>25, trend UP confirmed)")
        else:
            _add("bear", "adx", z_adx, f"ADX={snapshot.adx:.1f} (>25, trend DOWN confirmed)")
    else:
        breakdown.append(f"ADX={snapshot.adx:.1f} (≤25, ranging)")

    # ── Signal 9: Funding Rate (microstructure) ───────────────────────────────
    if microstructure and microstructure.funding:
        f = microstructure.funding
        fr = f.funding_rate
        if fr > 0.0005:   # Longs heavily paid → squeeze risk → bearish
            _add("bear", "funding", min(fr / 0.0005, 2.0),
                 f"Funding={fr:.5f} (>0.05%, longs squeezable → BEAR)")
        elif fr < -0.0005:  # Shorts heavily paid → short squeeze → bullish
            _add("bull", "funding", min(-fr / 0.0005, 2.0),
                 f"Funding={fr:.5f} (<-0.05%, shorts squeezable → BULL)")
        else:
            breakdown.append(f"Funding={fr:.5f} (neutral)")
    else:
        breakdown.append("Funding: N/A")

    # ── Signal 10: Volume Profile / POC position ──────────────────────────────
    if volume_profile:
        vp = volume_profile
        if vp.price_below_value_area:
            _add("bull", "vp", abs(vp.poc_distance_pct) / 2.0,
                 f"Price below VAL=${vp.value_area_low:,.2f} (discount to value → BULL)")
        elif vp.price_above_value_area:
            _add("bear", "vp", abs(vp.poc_distance_pct) / 2.0,
                 f"Price above VAH=${vp.value_area_high:,.2f} (premium to value → BEAR)")
        else:
            breakdown.append(
                f"Price inside value area (VAL={vp.value_area_low:,.2f}–VAH={vp.value_area_high:,.2f})"
            )
    else:
        breakdown.append("Volume Profile: N/A")

    # ── Aggregate ─────────────────────────────────────────────────────────────
    if bull_score > bear_score:
        dominant: Literal["bullish", "bearish", "neutral"] = "bullish"
        raw_count = bull_raw
        wscore    = bull_score
    elif bear_score > bull_score:
        dominant  = "bearish"
        raw_count = bear_raw
        wscore    = bear_score
    else:
        dominant  = "neutral"
        raw_count = 0    # tied = no dominant count
        wscore    = 0.0  # tied = no dominant score → never passes threshold

    threshold = float(
        _settings.confluence_volatile_score
        if require_higher_confluence
        else _settings.confluence_min_score
    )
    passes = wscore >= threshold

    logger.info(
        f"Confluence [{regime.value}]: bull={bull_raw}({bull_score:.1f}) "
        f"bear={bear_raw}({bear_score:.1f}) dominant={dominant} "
        f"wscore={wscore:.1f} threshold={threshold:.1f} passes={passes}"
    )

    return ConfluentSignal(
        bullish_count=bull_raw,
        bearish_count=bear_raw,
        bullish_score=round(bull_score, 3),
        bearish_score=round(bear_score, 3),
        dominant_direction=dominant,
        score=raw_count,
        weighted_score=round(wscore, 3),
        passes_threshold=passes,
        adaptive_threshold=threshold,
        signal_breakdown=breakdown,
        regime_applied=regime.value,
    )
