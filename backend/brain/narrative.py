"""
Gemini NarrativeContext — reframed LLM role.

Instead of asking Gemini "buy or sell?", we ask it to synthesise news,
sentiment, and on-chain anomalies into a structured NarrativeContext.
This context is then used to MODIFY signal thresholds and confidence,
not to make the trading decision itself.

Why this is better than asking for buy/sell:
  - Gemini is a language model, not a probabilistic trading model.
    It is excellent at synthesising narratives and identifying risks.
    It is unreliable at quantitative decisions where small probability
    differences matter.
  - By separating narrative analysis from decision-making, we get the
    best of both worlds: LLM qualitative insights + deterministic rules.

NarrativeContext fields:
  overall_bias          — bullish / bearish / neutral macro narrative
  tail_risks            — list of specific risk events that could invalidate the trade
  catalysts             — list of specific upcoming events that support the trade
  invalidation_conditions — what would prove the thesis wrong
  confidence_modifier   — float -0.3 to +0.3 applied to final confidence score
  require_higher_confluence — if True, use 6/8 threshold instead of 4/8
  reasoning             — concise narrative explanation

Role in system: Called once per standard cycle after confluence passes.
The confidence_modifier and require_higher_confluence fields are applied
before the risk guard's confidence check.

Dependencies: google-genai, pydantic, loguru
"""

from __future__ import annotations

import json
import time
from typing import Literal

from google import genai
from google.genai import types as genai_types
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.config.settings import settings
from backend.data.sentiment import SentimentSnapshot

_client = genai.Client(api_key=settings.gemini_api_key)
_MODEL  = "gemini-2.0-flash-lite"

_SYSTEM_PROMPT = """You are a macro crypto market analyst. Your job is NOT to make trading decisions.
Your job is to synthesise market narrative, identify tail risks, and assess whether current
conditions support or undermine a potential trade.

You will receive current market indicators, funding data, sentiment, and recent news.
Return ONLY valid JSON with this exact structure:
{
  "overall_bias": "bullish" | "bearish" | "neutral",
  "tail_risks": ["risk1", "risk2"],
  "catalysts": ["catalyst1", "catalyst2"],
  "invalidation_conditions": ["condition1", "condition2"],
  "confidence_modifier": <float between -0.30 and 0.30>,
  "require_higher_confluence": <true | false>,
  "reasoning": "<2-3 sentence summary>"
}

confidence_modifier rules:
  +0.10 to +0.30 = strong narrative support (multiple catalysts, low risk)
  -0.10 to -0.30 = narrative headwinds (high tail risk, unclear conditions)
  0.00            = neutral — narrative neither supports nor undermines

require_higher_confluence: set true if:
  - Major macro event within 24h (Fed, CPI, earnings)
  - Extreme funding rate (>0.05% or <-0.05%)
  - Liquidation cascade risk detected
  - News sentiment is strongly negative/conflicting

No markdown. No preamble. JSON only."""


class NarrativeContext(BaseModel):
    """
    Qualitative market narrative from Gemini used to modify signal thresholds.

    Attributes:
        overall_bias: Macro narrative direction — bullish / bearish / neutral.
        tail_risks: Specific events that could invalidate the trade.
        catalysts: Upcoming events supporting the trade direction.
        invalidation_conditions: Conditions that would prove the thesis wrong.
        confidence_modifier: Applied to final_confidence before risk check (-0.3 to +0.3).
        require_higher_confluence: If True, confluence threshold raised to 6/8.
        reasoning: 2-3 sentence narrative summary.

    Example:
        >>> ctx = get_narrative(snapshot, sentiment, microstructure)
        >>> adjusted_conf = base_conf + ctx.confidence_modifier
    """

    model_config = ConfigDict(frozen=True)

    overall_bias: Literal["bullish", "bearish", "neutral"] = "neutral"
    tail_risks: list[str] = Field(default_factory=list)
    catalysts: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    confidence_modifier: float = Field(default=0.0, ge=-0.3, le=0.3)
    require_higher_confluence: bool = False
    reasoning: str = "No narrative available."

    @field_validator("confidence_modifier", mode="before")
    @classmethod
    def clamp_modifier(cls, v: float) -> float:
        return max(-0.3, min(0.3, float(v)))

    @field_validator("overall_bias", mode="before")
    @classmethod
    def normalise_bias(cls, v: str) -> str:
        v = str(v).lower().strip()
        return v if v in ("bullish", "bearish", "neutral") else "neutral"


# Safe fallback returned on any API error
_NEUTRAL_NARRATIVE = NarrativeContext(
    overall_bias="neutral",
    tail_risks=[],
    catalysts=[],
    invalidation_conditions=[],
    confidence_modifier=0.0,
    require_higher_confluence=False,
    reasoning="Narrative analysis unavailable — using neutral defaults.",
)


def _build_prompt(
    price: float,
    rsi: float,
    confluence_score: int,
    confluence_direction: str,
    regime: str,
    funding_rate: float,
    funding_sentiment: str,
    ls_bias: str,
    fear_greed: int,
    news_sentiment: str,
    headlines: list[str],
    risk_regime: str,
) -> str:
    headline_str = " | ".join(headlines[:3]) if headlines else "No recent headlines."
    return f"""CURRENT MARKET SNAPSHOT:
- BTC/USD Price: ${price:,.2f}
- RSI(14): {rsi:.1f}
- Confluence Score: {confluence_score}/8 ({confluence_direction})
- Market Regime: {regime}

MICROSTRUCTURE:
- Funding Rate: {funding_rate:.5f} ({funding_sentiment})
- Long/Short Bias: {ls_bias}
- BTC Macro Regime: {risk_regime}

SENTIMENT:
- Fear & Greed: {fear_greed}/100
- News Sentiment: {news_sentiment}
- Headlines: {headline_str}

Analyse the current macro narrative and return the JSON context object."""


def get_narrative(
    price: float,
    rsi: float,
    confluence_score: int,
    confluence_direction: str,
    regime: str,
    sentiment: SentimentSnapshot,
    funding_rate: float = 0.0,
    funding_sentiment: str = "neutral",
    ls_bias: str = "balanced",
    risk_regime: str = "decorrelated",
) -> NarrativeContext:
    """
    Request a NarrativeContext from Gemini.

    This is intentionally fast — the prompt is short and Gemini returns
    structured JSON. Typical latency 300-800ms.

    Args:
        price: Current BTC/USD price.
        rsi: Current RSI value.
        confluence_score: Current confluence score (0-8).
        confluence_direction: 'bullish', 'bearish', or 'neutral'.
        regime: Current market regime string.
        sentiment: SentimentSnapshot from sentiment module.
        funding_rate: Current perpetual funding rate.
        funding_sentiment: 'bullish_squeeze', 'bearish_squeeze', or 'neutral'.
        ls_bias: Long/short bias string.
        risk_regime: Macro risk regime from correlations.

    Returns:
        NarrativeContext. Returns neutral fallback on any error.

    Example:
        >>> ctx = get_narrative(67000, 52.3, 5, 'bullish', 'TRENDING_UP_STRONG', sentiment)
        >>> final_conf = rule_conf + ctx.confidence_modifier
    """
    headlines = [item.title for item in sentiment.news_items[:3]] if sentiment.news_items else []

    prompt = _build_prompt(
        price=price,
        rsi=rsi,
        confluence_score=confluence_score,
        confluence_direction=confluence_direction,
        regime=regime,
        funding_rate=funding_rate,
        funding_sentiment=funding_sentiment,
        ls_bias=ls_bias,
        fear_greed=sentiment.fear_greed_value,
        news_sentiment=sentiment.overall_news_sentiment,
        headlines=headlines,
        risk_regime=risk_regime,
    )

    full_prompt = _SYSTEM_PROMPT + "\n\n" + prompt
    start = time.perf_counter()

    try:
        response = _client.models.generate_content(
            model=_MODEL,
            contents=full_prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json",
            ),
        )
        latency_ms = (time.perf_counter() - start) * 1000
        raw = response.text.strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            start_idx = raw.find("{")
            end_idx   = raw.rfind("}") + 1
            if start_idx != -1 and end_idx > start_idx:
                data = json.loads(raw[start_idx:end_idx])
            else:
                raise ValueError(f"Cannot parse JSON from Gemini narrative: {raw[:200]}")

        ctx = NarrativeContext(**data)
        logger.info(
            f"Narrative: bias={ctx.overall_bias} conf_mod={ctx.confidence_modifier:+.2f} "
            f"higher_confluence={ctx.require_higher_confluence} latency={latency_ms:.0f}ms"
        )
        return ctx

    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        logger.warning(f"Narrative API error after {latency_ms:.0f}ms: {exc}. Using neutral fallback.")
        return _NEUTRAL_NARRATIVE
