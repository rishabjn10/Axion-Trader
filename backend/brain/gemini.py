"""
Google Gemini Flash LLM decision engine.

Updated to use the new `google-genai` SDK (google.genai) which replaced the
deprecated `google-generativeai` package.

Role in system: Called once per standard cycle. Its decision is aggregated
with the rule engine's decision in aggregator.py.

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

# ── Gemini client setup ────────────────────────────────────────────────────────
_client = genai.Client(api_key=settings.gemini_api_key)
_MODEL_NAME = "gemini-3.1-flash-lite-preview"

# ── System prompt ──────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = (
    "You are an expert crypto trading analyst with deep knowledge of technical analysis, "
    "market microstructure, and risk management. "
    "You will be given current market indicators, sentiment data, and a summary of recent "
    "trading performance. Based on this data, you must decide whether to buy, sell, or hold. "
    "Always respond with valid JSON only. No markdown. No preamble. No explanation outside the JSON. "
    'Return exactly this structure: {"action": "buy"|"sell"|"hold", "confidence": 0.0-1.0, '
    '"reasoning": "your analysis here", "risk_assessment": "your risk evaluation here"}'
)


class MarketSnapshot(BaseModel):
    """Complete market context passed to the Gemini LLM for decision-making."""

    model_config = ConfigDict(frozen=False)

    pair: str
    current_price: float
    rsi: float
    macd_cross: str
    macd_histogram: float
    bb_pct_b: float
    bb_upper: float
    bb_lower: float
    vwap: float
    ema_fast: float
    ema_slow: float
    ema_cross: str
    atr: float
    adx: float
    confluence_score: int
    confluence_direction: str
    signal_breakdown: list[str] = Field(default_factory=list)
    regime: str
    fear_greed_value: int
    fear_greed_label: str
    news_sentiment: str
    top_headlines: list[str] = Field(default_factory=list)


class GeminiDecision(BaseModel):
    """Validated trading decision returned by the Gemini LLM."""

    model_config = ConfigDict(frozen=True)

    action: Literal["buy", "sell", "hold"] = "hold"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reasoning: str = "No reasoning provided."
    risk_assessment: str = "No risk assessment provided."

    @field_validator("confidence", mode="before")
    @classmethod
    def clamp_confidence(cls, v: float) -> float:
        return max(0.0, min(1.0, float(v)))

    @field_validator("action", mode="before")
    @classmethod
    def normalise_action(cls, v: str) -> str:
        v = str(v).lower().strip()
        if v not in ("buy", "sell", "hold"):
            logger.warning(f"Gemini returned invalid action '{v}', defaulting to 'hold'")
            return "hold"
        return v


def _build_user_prompt(snapshot: MarketSnapshot, reflection: str) -> str:
    """Build the structured user prompt from market data."""
    headlines_str = " | ".join(snapshot.top_headlines[:3]) if snapshot.top_headlines else "No recent headlines"

    return f"""
MARKET DATA FOR {snapshot.pair}:
- Current Price: ${snapshot.current_price:,.2f}
- RSI (14): {snapshot.rsi:.2f} {'(oversold)' if snapshot.rsi < 30 else '(overbought)' if snapshot.rsi > 70 else '(neutral)'}
- MACD Cross Direction: {snapshot.macd_cross} | Histogram: {snapshot.macd_histogram:.4f}
- Bollinger %B: {snapshot.bb_pct_b:.3f} | Upper: ${snapshot.bb_upper:,.2f} | Lower: ${snapshot.bb_lower:,.2f}
- VWAP: ${snapshot.vwap:,.2f} | Price vs VWAP: {'ABOVE' if snapshot.current_price > snapshot.vwap else 'BELOW'}
- EMA9: ${snapshot.ema_fast:,.2f} | EMA21: ${snapshot.ema_slow:,.2f} | Cross: {snapshot.ema_cross}
- ATR (14): ${snapshot.atr:.2f} | ADX (14): {snapshot.adx:.1f}

CONFLUENCE ANALYSIS ({snapshot.confluence_score}/8 — {snapshot.confluence_direction}):
{chr(10).join(f"  {line}" for line in snapshot.signal_breakdown) if snapshot.signal_breakdown else "  No breakdown available"}

MARKET REGIME: {snapshot.regime}

SENTIMENT:
- Fear & Greed Index: {snapshot.fear_greed_value}/100 ({snapshot.fear_greed_label})
- News Sentiment: {snapshot.news_sentiment}
- Recent Headlines: {headlines_str}

PAST PERFORMANCE (memory):
{reflection if reflection else "No previous trades on record."}

Based on this complete analysis, provide your trading decision as JSON.
""".strip()


def get_decision(snapshot: MarketSnapshot, reflection: str) -> GeminiDecision:
    """
    Request a trading decision from the Gemini 3.1 Flash Lite Preview LLM.

    Args:
        snapshot: Complete market context.
        reflection: Formatted string of the last N trades for memory context.

    Returns:
        GeminiDecision. On any error returns a safe hold decision.

    Example:
        >>> decision = get_decision(market_snapshot, reflection_context)
    """
    user_prompt = _build_user_prompt(snapshot, reflection)
    full_prompt = _SYSTEM_PROMPT + "\n\n" + user_prompt
    start_time = time.perf_counter()

    logger.debug(
        "━━━ GEMINI PROMPT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Model : {_MODEL_NAME}\n"
        f"Pair  : {snapshot.pair}\n"
        f"System: {_SYSTEM_PROMPT[:120]}…\n"
        "── User prompt ──────────────────────────────────────\n"
        f"{user_prompt}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    try:
        response = _client.models.generate_content(
            model=_MODEL_NAME,
            contents=full_prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json",
            ),
        )

        latency_ms = (time.perf_counter() - start_time) * 1000
        raw_text = response.text.strip()

        tokens_in = 0
        tokens_out = 0
        tokens_total = 0
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            um = response.usage_metadata
            tokens_in = getattr(um, "prompt_token_count", 0)
            tokens_out = getattr(um, "candidates_token_count", 0)
            tokens_total = getattr(um, "total_token_count", 0)

        logger.debug(
            "━━━ GEMINI RESPONSE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Latency : {latency_ms:.0f}ms\n"
            f"Tokens  : {tokens_in} in / {tokens_out} out / {tokens_total} total\n"
            "── Raw JSON ─────────────────────────────────────────\n"
            f"{raw_text}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            start_idx = raw_text.find("{")
            end_idx = raw_text.rfind("}") + 1
            if start_idx != -1 and end_idx > start_idx:
                data = json.loads(raw_text[start_idx:end_idx])
            else:
                raise ValueError(f"Could not extract JSON from response: {raw_text[:200]}")

        decision = GeminiDecision(**data)

        logger.info(
            f"Gemini decision: {decision.action.upper()} "
            f"(confidence={decision.confidence:.2f}) | "
            f"latency={latency_ms:.0f}ms | tokens={tokens_total}"
        )
        return decision

    except Exception as exc:
        latency_ms = (time.perf_counter() - start_time) * 1000
        logger.error(
            f"Gemini API error after {latency_ms:.0f}ms: {exc}. Defaulting to HOLD."
        )
        return GeminiDecision(
            action="hold",
            confidence=0.0,
            reasoning=f"API error — defaulting to hold. Error: {str(exc)[:200]}",
            risk_assessment="Unknown — API unavailable.",
        )
