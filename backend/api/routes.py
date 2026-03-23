"""
FastAPI route handlers for all axion-trader API endpoints.

Implements seven endpoints consumed by the React dashboard:

  GET  /api/health        — Liveness check with uptime
  GET  /api/state         — Agent status, last decision, circuit breaker
  GET  /api/trades        — Paginated trade history
  GET  /api/decisions     — Paginated decision history with AI reasoning
  GET  /api/metrics       — Portfolio performance metrics
  GET  /api/price         — Current live price from Kraken
  POST /api/mode          — Switch between paper and live mode

All endpoints return fully-typed Pydantic response models and use async def
throughout. Database reads use the memory.store module; price data uses the
data.fetcher module.

Role in system: HTTP interface layer. Reads data from SQLite and Kraken CLI,
formats it as JSON, and serves it to the React dashboard via polling hooks.

Dependencies: fastapi, pydantic, loguru, memory.store, data.fetcher
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from backend.config.settings import settings

router = APIRouter(prefix="/api", tags=["axion-trader"])


# ── Response Models ────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    """Health check response."""
    model_config = ConfigDict(frozen=True)
    status: str
    version: str
    mode: str
    uptime_seconds: int


class LastDecision(BaseModel):
    """Summary of the most recent trading decision."""
    model_config = ConfigDict(frozen=True)
    action: str
    confidence: float
    reasoning: str
    timestamp: str


class AgentStateResponse(BaseModel):
    """Complete agent state for dashboard overview."""
    model_config = ConfigDict(frozen=True)
    status: Literal["running", "paused", "halted", "circuit_breaker"]
    mode: Literal["paper", "live"]
    pair: str
    last_decision: LastDecision | None
    next_cycle_in_seconds: int
    circuit_breaker_active: bool
    circuit_breaker_recovery_time: str | None
    regime: str
    shock_guard_active: bool
    last_price: float


class TradeResponse(BaseModel):
    """Individual trade record for the trade log table."""
    model_config = ConfigDict(frozen=True)
    order_id: str
    timestamp: str
    action: str
    pair: str
    volume: float
    entry_price: float
    exit_price: float | None
    pnl_usd: float | None
    pnl_pct: float | None
    status: str
    mode: str
    stop_price: float
    take_profit_price: float
    llm_reasoning: str | None


class MetricsResponse(BaseModel):
    """Portfolio performance metrics for the dashboard metrics bar."""
    model_config = ConfigDict(frozen=True)
    total_pnl_usd: float
    total_pnl_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    win_rate_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    open_positions: int
    portfolio_value_usd: float
    daily_pnl_usd: float
    daily_pnl_pct: float


class DecisionResponse(BaseModel):
    """Individual decision record including AI reasoning."""
    model_config = ConfigDict(frozen=True)
    id: int
    timestamp: str
    final_action: str
    final_confidence: float
    llm_action: str | None
    llm_reasoning: str | None
    rule_action: str | None
    rule_triggered: str | None
    consensus_reached: bool
    approved_by_risk: bool
    confluence_score: int | None
    risk_rejection_reason: str | None
    rsi: float | None
    macd_cross: str | None
    bb_position: str | None
    confluence_breakdown: list[str] | None = None


class PriceResponse(BaseModel):
    """Current live price data from Kraken."""
    model_config = ConfigDict(frozen=True)
    pair: str
    current_price: float
    high_24h: float
    low_24h: float
    volume_24h: float
    timestamp: str


class ModeRequest(BaseModel):
    """Request body for mode switching."""
    mode: Literal["paper", "live"]


class ModeResponse(BaseModel):
    """Response after mode change."""
    model_config = ConfigDict(frozen=True)
    success: bool
    mode: str
    message: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/health", response_model=HealthResponse, summary="API health check")
async def get_health() -> HealthResponse:
    """
    Liveness check endpoint.

    Returns server status, version, current mode, and uptime in seconds.
    Used by monitoring systems and the dashboard connection status indicator.

    Returns:
        HealthResponse with status='ok', version, mode, and uptime_seconds.
    """
    from backend.api.app import START_TIME
    uptime = int(time.time() - START_TIME)

    return HealthResponse(
        status="ok",
        version="0.1.0",
        mode=settings.trading_mode,
        uptime_seconds=uptime,
    )


@router.get("/state", response_model=AgentStateResponse, summary="Agent status and last decision")
async def get_state() -> AgentStateResponse:
    """
    Return the complete agent state for the dashboard overview panel.

    Combines data from SQLite state table, recent decisions, and the
    shock guard singleton to provide a full picture of agent health.

    Returns:
        AgentStateResponse with status, mode, last decision, and system health.
    """
    from backend.execution.shock_guard import shock_guard
    from backend.memory.store import get_recent_decisions, get_state as db_get_state
    from backend.risk.guard import is_circuit_breaker_active

    # Read circuit breaker state
    cb_active = is_circuit_breaker_active()
    cb_recovery = db_get_state("circuit_breaker_recovery_time") if cb_active else None

    # Determine agent status
    agent_status_raw = db_get_state("agent_status") or "running"
    if cb_active:
        agent_status: Literal["running", "paused", "halted", "circuit_breaker"] = "circuit_breaker"
    elif agent_status_raw == "halted":
        agent_status = "halted"
    elif agent_status_raw == "paused":
        agent_status = "paused"
    else:
        agent_status = "running"

    # Get most recent standard-loop decision (has LLM reasoning).
    # Fast loop records (timeframe=15) are excluded — they have no llm_reasoning
    # and would otherwise always appear as the "last decision" on the dashboard.
    last_decision: LastDecision | None = None
    decisions = get_recent_decisions(limit=20)
    std_decisions = [d for d in decisions if d.get("timeframe") != 15 and d.get("llm_reasoning")]
    if std_decisions:
        d = std_decisions[0]
        last_decision = LastDecision(
            action=d.get("final_action", "hold"),
            confidence=float(d.get("final_confidence") or 0.0),
            reasoning=d.get("llm_reasoning") or "No reasoning recorded",
            timestamp=d.get("timestamp", ""),
        )

    # Read regime and next cycle info from state
    regime = db_get_state("current_regime") or "UNKNOWN"
    next_cycle_str = db_get_state("next_cycle_timestamp")
    if next_cycle_str:
        try:
            next_cycle_dt = datetime.fromisoformat(next_cycle_str)
            now = datetime.now(UTC)
            diff = int((next_cycle_dt - now).total_seconds())
            next_cycle_seconds = max(0, diff)
        except ValueError:
            next_cycle_seconds = 0
    else:
        next_cycle_seconds = 3600  # Default 1h if not set

    # Get last price from shock guard or state
    last_price = shock_guard.last_price
    if last_price == 0.0:
        last_price_str = db_get_state("last_price")
        if last_price_str:
            try:
                last_price = float(last_price_str)
            except ValueError:
                last_price = 0.0

    return AgentStateResponse(
        status=agent_status,
        mode=settings.trading_mode,
        pair=settings.trading_pair,
        last_decision=last_decision,
        next_cycle_in_seconds=next_cycle_seconds,
        circuit_breaker_active=cb_active,
        circuit_breaker_recovery_time=cb_recovery,
        regime=regime,
        shock_guard_active=shock_guard.is_running,
        last_price=last_price,
    )


@router.get("/trades", response_model=list[TradeResponse], summary="Trade history")
async def get_trades(
    limit: int = Query(default=50, ge=1, le=500, description="Maximum trades to return"),
) -> list[TradeResponse]:
    """
    Return paginated trade history ordered newest-first.

    Includes all trades regardless of status (open, closed, failed).
    Each record includes entry/exit prices, PnL, and AI reasoning.

    Args:
        limit: Number of trades to return (1–500). Defaults to 50.

    Returns:
        List of TradeResponse objects, newest first.
    """
    from backend.memory.store import get_recent_trades

    trades = get_recent_trades(limit=limit)
    return [
        TradeResponse(
            order_id=t.get("order_id", ""),
            timestamp=t.get("timestamp", ""),
            action=t.get("action", ""),
            pair=t.get("pair", settings.trading_pair),
            volume=float(t.get("volume") or 0.0),
            entry_price=float(t.get("entry_price") or 0.0),
            exit_price=float(t["exit_price"]) if t.get("exit_price") is not None else None,
            pnl_usd=float(t["pnl_usd"]) if t.get("pnl_usd") is not None else None,
            pnl_pct=float(t["pnl_pct"]) if t.get("pnl_pct") is not None else None,
            status=t.get("status", "unknown"),
            mode=t.get("mode", "paper"),
            stop_price=float(t.get("stop_price") or 0.0),
            take_profit_price=float(t.get("take_profit_price") or 0.0),
            llm_reasoning=t.get("llm_reasoning"),
        )
        for t in trades
    ]


@router.get("/metrics", response_model=MetricsResponse, summary="Portfolio performance metrics")
async def get_metrics() -> MetricsResponse:
    """
    Return computed portfolio performance metrics.

    Calculates Sharpe ratio, max drawdown, win rate, and PnL from the
    complete trade history in SQLite. When no portfolio snapshots exist yet
    (agent hasn't executed a full cycle), falls back to the current live
    account balance so the dashboard shows a meaningful starting value.

    Returns:
        MetricsResponse with all performance statistics.
    """
    from backend.memory.store import compute_metrics

    metrics = compute_metrics()

    # If no portfolio snapshots yet, seed portfolio_value from live balance
    if metrics["portfolio_value_usd"] == 0.0:
        try:
            from backend.data.fetcher import fetch_balance
            balances = fetch_balance()
            usd = balances.get("USD", balances.get("ZUSD", 0.0))
            if usd > 0:
                metrics["portfolio_value_usd"] = usd
        except Exception:
            pass

    return MetricsResponse(**metrics)


@router.get("/decisions", response_model=list[DecisionResponse], summary="Decision history")
async def get_decisions(
    limit: int = Query(default=100, ge=1, le=1000, description="Maximum decisions to return"),
) -> list[DecisionResponse]:
    """
    Return paginated decision history ordered newest-first.

    Includes every trading cycle's decision record with AI reasoning,
    rule engine output, consensus result, and risk approval status.

    Args:
        limit: Number of decisions to return (1–1000). Defaults to 100.

    Returns:
        List of DecisionResponse objects, newest first.
    """
    from backend.memory.store import get_recent_decisions

    decisions = get_recent_decisions(limit=limit)
    return [
        DecisionResponse(
            id=int(d.get("id", 0)),
            timestamp=d.get("timestamp", ""),
            final_action=d.get("final_action", "hold"),
            final_confidence=float(d.get("final_confidence") or 0.0),
            llm_action=d.get("llm_action"),
            llm_reasoning=d.get("llm_reasoning"),
            rule_action=d.get("rule_action"),
            rule_triggered=d.get("rule_triggered"),
            consensus_reached=bool(d.get("consensus_reached", 0)),
            approved_by_risk=bool(d.get("approved_by_risk", 0)),
            confluence_score=d.get("confluence_score"),
            risk_rejection_reason=d.get("risk_rejection_reason"),
            rsi=d.get("rsi"),
            macd_cross=d.get("macd_cross"),
            bb_position=d.get("bb_position"),
            confluence_breakdown=json.loads(d.get("confluence_breakdown") or "[]") or None,
        )
        for d in decisions
    ]


@router.get("/price", response_model=PriceResponse, summary="Current live price")
async def get_price() -> PriceResponse:
    """
    Fetch the current live price from Kraken for the configured trading pair.

    Calls the Kraken CLI ticker command. Returns cached data from the shock
    guard if the CLI call fails to ensure dashboard responsiveness.

    Returns:
        PriceResponse with current price, 24h high/low, and volume.

    Raises:
        HTTPException 503: If Kraken CLI is unavailable and no cached price exists.
    """
    from backend.data.fetcher import fetch_ticker
    from backend.execution.shock_guard import shock_guard

    try:
        ticker = fetch_ticker(settings.trading_pair)
        return PriceResponse(
            pair=settings.trading_pair,
            current_price=float(ticker.get("last_price", 0.0)),
            high_24h=float(ticker.get("high_24h", 0.0)),
            low_24h=float(ticker.get("low_24h", 0.0)),
            volume_24h=float(ticker.get("volume_24h", 0.0)),
            timestamp=ticker.get("timestamp", datetime.now(UTC).isoformat()),
        )
    except Exception as exc:
        logger.warning(f"Ticker fetch failed: {exc}. Using shock guard last price.")

        # Fall back to shock guard last price
        last_price = shock_guard.last_price
        if last_price == 0.0:
            raise HTTPException(
                status_code=503,
                detail=f"Price data unavailable: {exc}",
            )

        return PriceResponse(
            pair=settings.trading_pair,
            current_price=last_price,
            high_24h=last_price,
            low_24h=last_price,
            volume_24h=0.0,
            timestamp=shock_guard.last_update or datetime.now(UTC).isoformat(),
        )


@router.get("/ohlcv", summary="Recent OHLCV candle data for price chart")
async def get_ohlcv(
    interval: int = Query(default=60, ge=1, le=1440, description="Candle interval in minutes"),
    limit: int = Query(default=80, ge=10, le=500, description="Number of candles to return"),
) -> list[dict[str, Any]]:
    """
    Return recent OHLCV candles for the configured trading pair.

    Used by the price chart component to show real historical price data
    without needing trades to exist first.

    Args:
        interval: Candle interval in minutes. Defaults to 60.
        limit: Max candles to return (newest last). Defaults to 80.

    Returns:
        List of dicts with keys: time (ISO string), price (close), high, low, volume.
    """
    from backend.data.fetcher import fetch_ohlcv

    try:
        df = fetch_ohlcv(settings.trading_pair, interval)
        rows = df.tail(limit)
        result = []
        for _, row in rows.iterrows():
            ts = row["timestamp"]
            ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            result.append({
                "time": ts_str,
                "price": round(float(row["close"]), 2),
                "high": round(float(row["high"]), 2),
                "low": round(float(row["low"]), 2),
                "volume": round(float(row["volume"]), 6),
            })
        return result
    except Exception as exc:
        logger.warning(f"OHLCV chart fetch failed: {exc}")
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/mode", response_model=ModeResponse, summary="Switch trading mode")
async def set_mode(request: ModeRequest) -> ModeResponse:
    """
    Switch between paper and live trading modes.

    Updates the TRADING_MODE in the agent_state SQLite table. The agent
    main loop reads this on the next cycle and adjusts order routing.
    Note: changing to 'live' requires valid KRAKEN_API_KEY_TRADING to be set.

    Args:
        request: ModeRequest with the desired mode ('paper' or 'live').

    Returns:
        ModeResponse confirming the mode change.

    Raises:
        HTTPException 400: If switching to live mode with invalid credentials.
    """
    from backend.memory.store import set_state

    new_mode = request.mode

    # Validate live mode credentials before allowing switch
    if new_mode == "live":
        placeholder_patterns = {"your_", "placeholder", "example"}
        key = settings.kraken_api_key_trading
        if any(key.lower().startswith(p) for p in placeholder_patterns):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Cannot switch to LIVE mode: KRAKEN_API_KEY_TRADING appears to be "
                    "a placeholder value. Set real trading credentials in .env first."
                ),
            )

    set_state("trading_mode_override", new_mode)
    logger.info(f"Trading mode changed to: {new_mode.upper()}")

    return ModeResponse(
        success=True,
        mode=new_mode,
        message=f"Trading mode switched to {new_mode.upper()}. Takes effect on next cycle.",
    )
