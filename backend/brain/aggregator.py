"""
Hybrid consensus aggregator — combines LLM and rule engine decisions.

The aggregator is the final arbiter between the two independent trading
signals: the Gemini LLM (strategic/narrative) and the rule engine
(tactical/deterministic). Its job is to produce a single final decision
while maintaining strong risk discipline.

Consensus logic:
1. **Full consensus**: Both LLM and rules agree on the SAME non-hold action.
   → Execute with confidence = mean(llm_confidence, rule_confidence).
2. **Disagreement or either holds**: Any disagreement or hold from either side.
   → Default to HOLD. Two different views = insufficient conviction.
3. **LLM override (rules silent)**: Rule engine triggered no rule (confidence=0.0),
   but LLM confidence exceeds CONFIDENCE_THRESHOLD.
   → Pass through LLM decision since rules simply have no opinion (not a disagreement).
4. **Rules override (LLM holds)**: LLM says hold but a high-confidence rule fired.
   → Hold anyway. The LLM's narrative context may be seeing something the rules miss.

Why this conservative approach? In trading, missing a good trade costs
opportunity. Making a bad trade costs real money. The asymmetry means
we should err on the side of caution when signals disagree.

Role in system: Called once per standard cycle after both Gemini and rules
have produced their individual decisions.

Dependencies: pydantic, loguru
"""

from __future__ import annotations

from typing import Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from backend.brain.gemini import GeminiDecision
from backend.brain.rules import RuleDecision
from backend.config.settings import settings


class FinalDecision(BaseModel):
    """
    The aggregated final trading decision from both brain systems.

    Attributes:
        action: Final recommended action: 'buy', 'sell', or 'hold'.
        final_confidence: Combined confidence score (mean of both, or single source).
        llm_action: What the Gemini LLM recommended.
        llm_confidence: Gemini's confidence.
        rule_action: What the rule engine recommended.
        rule_confidence: Rule engine's confidence.
        consensus_reached: True if both systems agreed on a non-hold action.
        reasoning: Human-readable explanation of the aggregation outcome.

    Example:
        >>> final = aggregate(llm_decision, rule_decision)
        >>> print(final.action, final.consensus_reached)
    """

    model_config = ConfigDict(frozen=True)

    action: Literal["buy", "sell", "hold"]
    final_confidence: float = Field(ge=0.0, le=1.0)
    llm_action: str
    llm_confidence: float
    rule_action: str
    rule_confidence: float
    consensus_reached: bool
    reasoning: str


def aggregate(llm: GeminiDecision, rule: RuleDecision) -> FinalDecision:
    """
    Combine LLM and rule engine decisions into a single final decision.

    Implements a conservative consensus model where disagreement always results
    in a HOLD. Only when both systems independently agree on the same direction
    does the agent proceed to risk management and execution.

    The LLM override case (rule confidence=0.0) handles the situation where
    the rule engine simply found no setup matching its strict criteria — this
    is not a "hold" vote, it's abstention. In this case, we trust the LLM's
    judgement if its confidence is above the configured threshold.

    Args:
        llm: GeminiDecision from the Gemini Flash LLM call.
        rule: RuleDecision from the deterministic rule engine.

    Returns:
        FinalDecision with the aggregated outcome and full audit trail.

    Example:
        >>> final = aggregate(gemini_dec, rule_dec)
        >>> if final.action != 'hold' and final.consensus_reached:
        ...     proceed_to_risk_check(final)
    """
    llm_action = llm.action
    rule_action = rule.action
    llm_conf = llm.confidence
    rule_conf = rule.confidence

    logger.info(
        f"Aggregating: LLM={llm_action} ({llm_conf:.2f}), "
        f"Rules={rule_action} ({rule_conf:.2f}, rule={rule.triggered_rule})"
    )

    # ── Case 1: Full consensus — both agree on same non-hold action ───────────
    if (
        llm_action == rule_action
        and llm_action != "hold"
        and rule_action != "hold"
    ):
        final_conf = round((llm_conf + rule_conf) / 2, 4)
        reasoning = (
            f"CONSENSUS: Both LLM ({llm_action}, {llm_conf:.2f}) and rule engine "
            f"({rule_action}, {rule_conf:.2f}, rule='{rule.triggered_rule}') agree. "
            f"Final confidence: {final_conf:.2f}."
        )
        logger.info(f"Consensus reached: {llm_action} @ {final_conf:.2f}")
        return FinalDecision(
            action=llm_action,
            final_confidence=final_conf,
            llm_action=llm_action,
            llm_confidence=llm_conf,
            rule_action=rule_action,
            rule_confidence=rule_conf,
            consensus_reached=True,
            reasoning=reasoning,
        )

    # ── Case 2: LLM override — rules abstained (no rule triggered) ───────────
    # rule_conf == 0.0 means the rule engine abstained, not that it voted hold.
    # In this case, trust the LLM if it has high confidence.
    if rule_conf == 0.0 and llm_action != "hold" and llm_conf >= settings.confidence_threshold:
        reasoning = (
            f"LLM OVERRIDE: Rule engine abstained (no pattern matched). "
            f"LLM is confident: {llm_action} @ {llm_conf:.2f} ≥ threshold {settings.confidence_threshold}. "
            f"Proceeding on LLM signal only."
        )
        logger.info(f"LLM override: {llm_action} @ {llm_conf:.2f} (rules abstained)")
        return FinalDecision(
            action=llm_action,
            final_confidence=llm_conf,
            llm_action=llm_action,
            llm_confidence=llm_conf,
            rule_action=rule_action,
            rule_confidence=rule_conf,
            consensus_reached=False,
            reasoning=reasoning,
        )

    # ── Case 3: Disagreement — default to HOLD ────────────────────────────────
    # This covers all remaining cases:
    # - LLM says buy, rules say sell (or vice versa)
    # - LLM says hold, rules have a signal
    # - Rules have a signal, LLM has insufficient confidence
    if llm_action != rule_action:
        reasoning = (
            f"DISAGREEMENT: LLM={llm_action} ({llm_conf:.2f}) vs "
            f"rules={rule_action} ({rule_conf:.2f}, '{rule.triggered_rule}'). "
            f"Defaulting to HOLD — insufficient consensus."
        )
    elif llm_action == "hold" and rule_conf > 0.0:
        reasoning = (
            f"LLM HOLDS: Despite rule signal ({rule_action}, {rule_conf:.2f}), "
            f"LLM recommends hold — possible narrative concern. Defaulting to HOLD."
        )
    elif llm_action != "hold" and llm_conf < settings.confidence_threshold:
        reasoning = (
            f"LLM LOW CONFIDENCE: {llm_action} @ {llm_conf:.2f} below threshold "
            f"{settings.confidence_threshold}. Rule: '{rule.triggered_rule}'. Holding."
        )
    else:
        reasoning = (
            f"HOLD: No strong confluence between LLM ({llm_action}) "
            f"and rules ({rule_action}). Staying flat."
        )

    logger.info(f"No consensus → HOLD. Reason: {reasoning[:100]}")
    return FinalDecision(
        action="hold",
        final_confidence=0.0,
        llm_action=llm_action,
        llm_confidence=llm_conf,
        rule_action=rule_action,
        rule_confidence=rule_conf,
        consensus_reached=False,
        reasoning=reasoning,
    )
