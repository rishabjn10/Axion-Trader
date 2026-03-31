"""
Deterministic rule engine — fast, LLM-free trading signal generation.

The rule engine evaluates a fixed set of technical analysis rules in strict
priority order. Unlike the Gemini LLM, these rules are:
- Deterministic: same inputs always produce the same output
- Instant: no API call latency (sub-millisecond execution)
- Auditable: each decision includes the rule that triggered it
- Reliable: not subject to API outages or rate limiting

The rule engine is used in two ways:
1. **Fast loop (every 15 minutes)**: Rules run alone — if any rule fires with
   confidence >= 0.82, the agent executes immediately without waiting for
   the hourly Gemini cycle.
2. **Standard loop (every 60 minutes)**: Rules run alongside Gemini, and
   their decisions are aggregated in aggregator.py via consensus logic.

Rule priority order (first match wins — no multiple rules firing):
1. RSI < 28 AND price ≤ lower BB AND MACD bullish cross → BUY (0.82)
2. RSI > 72 AND price ≥ upper BB AND MACD bearish cross → SELL (0.82)
3. EMA9 crossed above EMA21 AND regime.is_bullish → BUY (0.78)
4. EMA9 crossed below EMA21 AND regime.is_bearish → SELL (0.78)
5. EMA9 > EMA21 AND MACD histogram > 0 AND RSI 40-65 AND regime.is_bullish → BUY (0.72)
6. EMA9 < EMA21 AND MACD histogram < 0 AND RSI 35-60 AND regime.is_bearish → SELL (0.72)
7. RSI < 35 AND price < VWAP AND MACD histogram > 0 AND regime = RANGING → BUY (0.70)
8. RSI > 65 AND price > VWAP AND MACD histogram < 0 AND regime = RANGING → SELL (0.70)
9. Default → HOLD (0.0, rule="no_rule_triggered")

Role in system: Tactical layer. Called by fast loop for immediate signals and
by standard loop for consensus with the LLM.

Dependencies: pydantic, loguru, indicators.engine, indicators.regime
"""

from __future__ import annotations

from typing import Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from backend.config.settings import settings
from backend.indicators.engine import IndicatorSnapshot
from backend.indicators.regime import MarketRegime


class RuleDecision(BaseModel):
    """
    Trading decision produced by the deterministic rule engine.

    Attributes:
        action: 'buy', 'sell', or 'hold'.
        confidence: Fixed confidence score for the triggered rule. 0.0 if no rule fired.
        triggered_rule: Machine-readable identifier of the rule that fired.
                        'no_rule_triggered' if the default was used.

    Example:
        >>> decision = evaluate(indicator_snap, MarketRegime.TRENDING_UP)
        >>> print(decision.triggered_rule, decision.confidence)
    """

    model_config = ConfigDict(frozen=True)

    action: Literal["buy", "sell", "hold"]
    confidence: float = Field(ge=0.0, le=1.0)
    triggered_rule: str


def evaluate(snapshot: IndicatorSnapshot, regime: MarketRegime) -> RuleDecision:
    """
    Evaluate the indicator snapshot against all trading rules in priority order.

    Checks each rule in sequence and returns immediately upon the first match.
    If no rule matches, returns the default HOLD decision.

    Rule 1 — RSI oversold + BB lower touch + MACD bullish cross:
        This three-confirmation rule targets high-probability mean reversion entries.
        RSI < 28 is deep oversold. Price at/below lower BB confirms oversold.
        MACD bullish cross confirms momentum is turning. All three together
        dramatically reduce false positives.

    Rule 2 — RSI overbought + BB upper touch + MACD bearish cross:
        Mirror of Rule 1 for short/sell entries. Targets overextended markets.

    Rule 3 — EMA9/21 bullish cross in uptrend:
        Trend-following entry. The EMA cross is a standard momentum signal.
        Only valid when regime is TRENDING_UP to avoid mean-reversion traps.

    Rule 4 — EMA9/21 bearish cross in downtrend:
        Mirror of Rule 3 for sell signals in TRENDING_DOWN regime.

    Args:
        snapshot: Computed indicator values for the current timeframe.
        regime: Detected market regime from regime.detect_regime().

    Returns:
        RuleDecision with action, confidence, and which rule fired.

    Example:
        >>> from backend.indicators.regime import MarketRegime
        >>> decision = evaluate(snap, MarketRegime.TRENDING_UP)
        >>> if decision.confidence > 0.80:
        ...     print(f"High-confidence rule: {decision.triggered_rule}")
    """

    # ── Rule 1: RSI oversold + lower BB + MACD bullish cross ──────────────────
    # Three independent confirmations pointing to the same direction:
    # deep oversold RSI, price testing support band, and fresh momentum cross
    rsi_oversold = snapshot.rsi < settings.rsi_oversold
    price_at_lower_bb = snapshot.current_price <= snapshot.bb_lower * 1.005  # Allow 0.5% tolerance
    macd_bullish_cross = snapshot.macd_cross_direction == "bullish"

    if rsi_oversold and price_at_lower_bb and macd_bullish_cross:
        logger.info(
            f"Rule 1 fired: RSI_oversold_BB_MACD | RSI={snapshot.rsi:.1f}, "
            f"price={snapshot.current_price:.2f} ≤ BB_lower={snapshot.bb_lower:.2f}, "
            f"MACD={snapshot.macd_cross_direction}"
        )
        return RuleDecision(
            action="buy",
            confidence=settings.rule_conf_extreme,
            triggered_rule="RSI_oversold_BB_MACD",
        )

    # ── Rule 2: RSI overbought + upper BB + MACD bearish cross ───────────────
    rsi_overbought = snapshot.rsi > settings.rsi_overbought
    price_at_upper_bb = snapshot.current_price >= snapshot.bb_upper * 0.995  # Allow 0.5% tolerance
    macd_bearish_cross = snapshot.macd_cross_direction == "bearish"

    if rsi_overbought and price_at_upper_bb and macd_bearish_cross:
        logger.info(
            f"Rule 2 fired: RSI_overbought_BB_MACD | RSI={snapshot.rsi:.1f}, "
            f"price={snapshot.current_price:.2f} ≥ BB_upper={snapshot.bb_upper:.2f}, "
            f"MACD={snapshot.macd_cross_direction}"
        )
        return RuleDecision(
            action="sell",
            confidence=settings.rule_conf_extreme,
            triggered_rule="RSI_overbought_BB_MACD",
        )

    # ── Rule 3: EMA bullish cross in any uptrend regime ──────────────────────
    # EMA cross is only used as an entry signal when the macro regime confirms
    # the trend direction. This prevents EMA crosses from triggering in ranging markets.
    # Accepts both TRENDING_UP_STRONG and TRENDING_UP_WEAK (regime.is_bullish).
    ema_bullish_cross = snapshot.ema_cross == "bullish"
    in_uptrend = regime.is_bullish

    if ema_bullish_cross and in_uptrend:
        logger.info(
            f"Rule 3 fired: EMA_cross_uptrend | EMA{9}={snapshot.ema_fast:.2f} crossed "
            f"above EMA{21}={snapshot.ema_slow:.2f} | regime={regime.value}"
        )
        return RuleDecision(
            action="buy",
            confidence=settings.rule_conf_cross,
            triggered_rule="EMA_cross_uptrend",
        )

    # ── Rule 4: EMA bearish cross in TRENDING_DOWN regime ────────────────────
    ema_bearish_cross = snapshot.ema_cross == "bearish"
    in_downtrend = regime.is_bearish  # MarketRegime.TRENDING_DOWN

    if ema_bearish_cross and in_downtrend:
        logger.info(
            f"Rule 4 fired: EMA_cross_downtrend | EMA{9}={snapshot.ema_fast:.2f} crossed "
            f"below EMA{21}={snapshot.ema_slow:.2f} | regime={regime.value}"
        )
        return RuleDecision(
            action="sell",
            confidence=settings.rule_conf_cross,
            triggered_rule="EMA_cross_downtrend",
        )

    # ── Rule 5: EMA bullish state + MACD positive momentum + RSI mid-range ──────
    # State-based (not event): EMA fast is ABOVE slow, MACD histogram is positive
    # and accelerating. RSI in 40-65 means not yet overbought — room to run.
    # Only in bullish regimes to avoid whipsaws in ranging/down markets.
    ema_bullish_state = snapshot.ema_fast > snapshot.ema_slow
    macd_positive     = snapshot.macd_histogram > 0
    rsi_mid_bull      = settings.rsi_bull_min <= snapshot.rsi <= settings.rsi_bull_max

    if ema_bullish_state and macd_positive and rsi_mid_bull and regime.is_bullish:
        logger.info(
            f"Rule 5 fired: EMA_momentum_bull | EMA9={snapshot.ema_fast:.2f} > "
            f"EMA21={snapshot.ema_slow:.2f}, MACD_hist={snapshot.macd_histogram:.4f}, "
            f"RSI={snapshot.rsi:.1f}, regime={regime.value}"
        )
        return RuleDecision(
            action="buy",
            confidence=settings.rule_conf_state,
            triggered_rule="EMA_momentum_bull",
        )

    # ── Rule 6: EMA bearish state + MACD negative momentum + RSI mid-range ──────
    ema_bearish_state = snapshot.ema_fast < snapshot.ema_slow
    macd_negative     = snapshot.macd_histogram < 0
    rsi_mid_bear      = settings.rsi_bear_min <= snapshot.rsi <= settings.rsi_bear_max

    if ema_bearish_state and macd_negative and rsi_mid_bear and regime.is_bearish:
        logger.info(
            f"Rule 6 fired: EMA_momentum_bear | EMA9={snapshot.ema_fast:.2f} < "
            f"EMA21={snapshot.ema_slow:.2f}, MACD_hist={snapshot.macd_histogram:.4f}, "
            f"RSI={snapshot.rsi:.1f}, regime={regime.value}"
        )
        return RuleDecision(
            action="sell",
            confidence=settings.rule_conf_state,
            triggered_rule="EMA_momentum_bear",
        )

    # ── Rule 7: RSI oversold bounce + price below VWAP in ranging market ─────────
    # In ranging markets, RSI < 35 near the lower end of a range often means
    # a bounce back to the mean is coming. VWAP below price confirms discount.
    # Positive MACD histogram confirms the momentum is already turning.
    rsi_oversold_soft = snapshot.rsi < settings.rsi_soft_oversold
    price_below_vwap  = snapshot.vwap > 0 and snapshot.current_price < snapshot.vwap
    in_ranging        = regime == MarketRegime.RANGING

    if rsi_oversold_soft and price_below_vwap and macd_positive and in_ranging:
        logger.info(
            f"Rule 7 fired: RSI_bounce_ranging | RSI={snapshot.rsi:.1f}, "
            f"price={snapshot.current_price:.2f} < VWAP={snapshot.vwap:.2f}, "
            f"MACD_hist={snapshot.macd_histogram:.4f}"
        )
        return RuleDecision(
            action="buy",
            confidence=settings.rule_conf_ranging,
            triggered_rule="RSI_bounce_ranging",
        )

    # ── Rule 8: RSI overbought fade + price above VWAP in ranging market ─────────
    rsi_overbought_soft = snapshot.rsi > settings.rsi_soft_overbought
    price_above_vwap    = snapshot.vwap > 0 and snapshot.current_price > snapshot.vwap

    if rsi_overbought_soft and price_above_vwap and macd_negative and in_ranging:
        logger.info(
            f"Rule 8 fired: RSI_fade_ranging | RSI={snapshot.rsi:.1f}, "
            f"price={snapshot.current_price:.2f} > VWAP={snapshot.vwap:.2f}, "
            f"MACD_hist={snapshot.macd_histogram:.4f}"
        )
        return RuleDecision(
            action="sell",
            confidence=settings.rule_conf_ranging,
            triggered_rule="RSI_fade_ranging",
        )

    # ── Default: no rule triggered ────────────────────────────────────────────
    logger.debug(
        f"No rule triggered | RSI={snapshot.rsi:.1f}, MACD={snapshot.macd_cross_direction}, "
        f"EMA cross={snapshot.ema_cross}, regime={regime.value}"
    )
    return RuleDecision(
        action="hold",
        confidence=0.0,
        triggered_rule="no_rule_triggered",
    )
