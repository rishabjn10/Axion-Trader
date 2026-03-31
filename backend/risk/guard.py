"""
Three-tier risk management guard — capital protection for axion-trader.

This module implements three completely independent risk checks that act as
sequential gates between a trading signal and actual order execution:

TIER 1 — Per-Trade Guard (approve_trade):
    Validates each individual potential trade. Checks confidence threshold,
    position sizing against portfolio value, and enforces minimum 2:1
    risk/reward ratio using ATR-based stop and take-profit levels.

TIER 2 — Portfolio Guard (check_portfolio):
    Validates the overall portfolio state. Prevents over-concentration by
    limiting total open positions and total portfolio exposure percentage.
    Called at the start of each cycle, not per-trade.

TIER 3 — Circuit Breaker (check_circuit_breaker):
    Halts ALL trading when daily losses exceed the configured threshold.
    State is persisted to SQLite so it survives agent restarts. Recovery
    time is set to the next calendar day at 00:00 UTC.

Design philosophy: Each tier is independent — they do not call each other.
The main loop calls them in sequence (Tier 2 → Tier 3 → Tier 1).
Any tier can independently block a trade without knowledge of the others.

Role in system: Called by main.py after the aggregator produces a non-hold
decision, and before any order is sent to the Kraken CLI.

Dependencies: pydantic, loguru, memory.store, config.settings
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from backend.config.settings import settings


# ── Kelly Criterion position sizing ───────────────────────────────────────────

def compute_kelly_position_pct(trades: list[dict]) -> float:
    """
    Compute Half Kelly position size from closed trade history.

    Half Kelly formula: f* = (win_rate × avg_win − loss_rate × avg_loss) / avg_win / 2

    Falls back to settings.max_position_pct when insufficient history.

    Args:
        trades: List of trade dicts from get_recent_trades(). Must have 'pnl_pct' key.

    Returns:
        Position size as fraction of portfolio (0.01–0.50).

    Example:
        >>> pos_pct = compute_kelly_position_pct(get_recent_trades(100))
        >>> position_usd = portfolio_value * pos_pct
    """
    closed = [t for t in trades if t.get("pnl_pct") is not None and t.get("status") == "closed"]

    if len(closed) < settings.kelly_min_trades:
        logger.debug(
            f"Kelly: only {len(closed)} closed trades (need {settings.kelly_min_trades}), "
            f"using default {settings.max_position_pct:.1%}"
        )
        return settings.max_position_pct

    wins   = [t["pnl_pct"] for t in closed if t["pnl_pct"] > 0]
    losses = [abs(t["pnl_pct"]) for t in closed if t["pnl_pct"] < 0]

    if not wins or not losses:
        return settings.max_position_pct

    win_rate  = len(wins) / len(closed)
    loss_rate = 1.0 - win_rate
    avg_win   = sum(wins)   / len(wins)   / 100.0  # convert % to fraction
    avg_loss  = sum(losses) / len(losses) / 100.0

    if avg_win <= 0:
        return settings.max_position_pct

    # Full Kelly = (W×b - L) / b  where b = avg_win / avg_loss
    # Simplified: (win_rate × avg_win − loss_rate × avg_loss) / avg_win
    full_kelly = (win_rate * avg_win - loss_rate * avg_loss) / avg_win
    half_kelly = full_kelly / 2.0

    # Clamp between 1% and settings.max_position_pct
    result = max(0.01, min(settings.max_position_pct, half_kelly))

    logger.info(
        f"Kelly sizing: win_rate={win_rate:.1%} avg_win={avg_win:.3%} avg_loss={avg_loss:.3%} "
        f"full_kelly={full_kelly:.3f} half_kelly={half_kelly:.3f} → clamped={result:.3f}"
    )
    return round(result, 4)


# ── Gradual circuit breaker recovery ──────────────────────────────────────────

def get_recovery_size_multiplier() -> float:
    """
    Return position size multiplier based on circuit breaker recovery day.

    Day 0 (not in recovery): 1.0×
    Day 1 after CB:          0.25×
    Day 2:                   0.50×
    Day 3:                   0.75×
    Day 4+:                  1.00×

    Returns:
        Multiplier to apply to computed position size.

    Example:
        >>> mult = get_recovery_size_multiplier()
        >>> position_usd = kelly_position_usd * mult
    """
    from backend.memory.store import get_state

    try:
        recovery_day_str = get_state("cb_recovery_day")
        if recovery_day_str is None:
            return 1.0  # Not in recovery

        recovery_day = int(recovery_day_str)
        if recovery_day <= 0:
            return 1.0
        if recovery_day == 1:
            mult = 0.25
        elif recovery_day == 2:
            mult = 0.50
        elif recovery_day == 3:
            mult = 0.75
        else:
            mult = 1.00

        if recovery_day >= 4:
            # Fully recovered — clear recovery state
            from backend.memory.store import set_state
            set_state("cb_recovery_day", "0")

        logger.info(f"CB recovery day {recovery_day}: position size multiplier = {mult:.2f}×")
        return mult

    except Exception as exc:
        logger.warning(f"Error reading CB recovery day: {exc}")
        return 1.0


class TradeApproval(BaseModel):
    """
    Result of the Tier 1 per-trade risk assessment.

    Attributes:
        approved: True if the trade passed all risk checks.
        reason: Human-readable explanation of the approval or rejection.
        position_size_usd: Approved position size in USD (0 if rejected).
        stop_price: Calculated stop-loss price level.
        take_profit_price: Calculated take-profit price level.

    Example:
        >>> approval = approve_trade('buy', 0.8, 67000.0, 100000.0, 1500.0)
        >>> if approval.approved:
        ...     place_order(volume=approval.position_size_usd / 67000.0)
    """

    model_config = ConfigDict(frozen=True)

    approved: bool
    reason: str
    position_size_usd: float = Field(default=0.0, ge=0.0)
    stop_price: float = Field(default=0.0, ge=0.0)
    take_profit_price: float = Field(default=0.0, ge=0.0)


def approve_trade(
    action: Literal["buy", "sell"],
    confidence: float,
    entry_price: float,
    portfolio_value: float,
    atr: float,
) -> TradeApproval:
    """
    Tier 1: Validate a single potential trade against risk parameters.

    Performs three checks in sequence:
    1. Confidence threshold — reject if below CONFIDENCE_THRESHOLD setting.
    2. Position sizing — calculate max position as MAX_POSITION_PCT * portfolio_value.
    3. Risk/reward ratio — verify take_profit / stop_distance >= 2.0.

    ATR-based stop/TP calculation:
    - Stop loss: entry ± (ATR * 1.5)  [1.5x ATR gives breathing room for volatility]
    - Take profit: entry ± (ATR * 3.0) [3x ATR = 2:1 reward/risk ratio]

    Args:
        action: 'buy' or 'sell' — determines stop/TP direction.
        confidence: Combined confidence score from aggregator (0.0–1.0).
        entry_price: Expected entry price (usually current market price).
        portfolio_value: Total portfolio value in USD for position sizing.
        atr: Current ATR value used for stop/TP calculation.

    Returns:
        TradeApproval with approved flag, reason, and calculated price levels.

    Example:
        >>> approval = approve_trade('buy', 0.82, 67000.0, 50000.0, 1200.0)
        >>> print(f"Stop: {approval.stop_price}, TP: {approval.take_profit_price}")
    """
    # ── Check 1: Confidence threshold ─────────────────────────────────────────
    if confidence < settings.confidence_threshold:
        reason = (
            f"Confidence {confidence:.2f} below threshold {settings.confidence_threshold}. "
            f"Trade rejected."
        )
        logger.warning(f"Tier 1 REJECTED: {reason}")
        return TradeApproval(approved=False, reason=reason)

    # ── Check 2: Position sizing (Kelly criterion + recovery multiplier) ─────
    if settings.use_kelly_sizing:
        try:
            from backend.memory.store import get_recent_trades
            recent = get_recent_trades(100)
            kelly_pct = compute_kelly_position_pct(recent)
        except Exception:
            kelly_pct = settings.max_position_pct
    else:
        kelly_pct = settings.max_position_pct

    recovery_mult = get_recovery_size_multiplier()
    effective_pct = kelly_pct * recovery_mult
    max_position_usd = effective_pct * portfolio_value
    position_size_usd = max_position_usd

    if position_size_usd <= 0:
        reason = f"Position size ${position_size_usd:.2f} is zero or negative (portfolio_value=${portfolio_value:.2f})"
        logger.warning(f"Tier 1 REJECTED: {reason}")
        return TradeApproval(approved=False, reason=reason)

    # ── Calculate stop and take-profit prices ─────────────────────────────────
    # Stop distance: max of ATR-based (adapts to volatility) and configured STOP_LOSS_PCT
    # (percentage floor guarantees the .env setting is always respected).
    # Take profit always maintains a minimum 2:1 R:R ratio relative to the stop.
    atr_stop = atr * settings.atr_stop_multiplier
    pct_stop = settings.stop_loss_pct * entry_price
    stop_distance = max(atr_stop, pct_stop)
    tp_distance = stop_distance * settings.tp_ratio

    logger.debug(
        f"Stop calc: ATR-based=${atr_stop:.2f} ({atr_stop/entry_price:.2%}), "
        f"STOP_LOSS_PCT=${pct_stop:.2f} ({settings.stop_loss_pct:.2%}), "
        f"using=${stop_distance:.2f} | TP=${tp_distance:.2f}"
    )

    if action == "buy":
        stop_price = entry_price - stop_distance
        take_profit_price = entry_price + tp_distance
    else:  # sell
        stop_price = entry_price + stop_distance
        take_profit_price = entry_price - tp_distance

    # ── Check 3: Risk/reward ratio validation ──────────────────────────────────
    # Both stop_distance and tp_distance must be > 0 for valid R:R calculation
    if stop_distance <= 0 or atr <= 0:
        reason = f"ATR={atr:.2f} is zero or negative — cannot calculate valid stop. Reject."
        logger.warning(f"Tier 1 REJECTED: {reason}")
        return TradeApproval(approved=False, reason=reason)

    # R:R ratio = potential profit / potential loss = tp_distance / stop_distance
    rr_ratio = tp_distance / stop_distance  # Should equal settings.tp_ratio
    if rr_ratio < settings.tp_ratio:
        reason = (
            f"Risk/reward ratio {rr_ratio:.2f}:1 below minimum {settings.tp_ratio:.1f}:1. "
            f"Stop={stop_distance:.2f}, TP={tp_distance:.2f}. Trade rejected."
        )
        logger.warning(f"Tier 1 REJECTED: {reason}")
        return TradeApproval(approved=False, reason=reason)

    # ── All checks passed ─────────────────────────────────────────────────────
    reason = (
        f"Trade approved: {action} @ ${entry_price:,.2f} | "
        f"Size=${position_size_usd:,.2f} | "
        f"Stop=${stop_price:,.2f} | TP=${take_profit_price:,.2f} | "
        f"R:R={rr_ratio:.1f}:1 | Confidence={confidence:.2f}"
    )
    logger.info(f"Tier 1 APPROVED: {reason}")

    return TradeApproval(
        approved=True,
        reason=reason,
        position_size_usd=round(position_size_usd, 2),
        stop_price=round(stop_price, 2),
        take_profit_price=round(take_profit_price, 2),
    )


def check_portfolio(open_positions: int, total_exposure_pct: float) -> tuple[bool, str]:
    """
    Tier 2: Check overall portfolio state before accepting new positions.

    Two independent portfolio-level checks:
    1. Open position count — enforce MAX_OPEN_POSITIONS cap.
    2. Total exposure — prevent over-leverage at portfolio level.

    This is called at the start of each cycle (before per-trade checks) so
    we fail fast if the portfolio is already fully deployed.

    Args:
        open_positions: Number of currently open positions across all pairs.
        total_exposure_pct: Total invested capital as a fraction of portfolio (0.0–1.0).
                            Example: 0.10 = 10% of portfolio is currently in open trades.

    Returns:
        Tuple of (allowed: bool, reason: str). If allowed=False, the cycle
        should be skipped and the reason logged.

    Example:
        >>> ok, reason = check_portfolio(1, 0.05)
        >>> if not ok:
        ...     logger.warning(f"Portfolio gate: {reason}")
    """
    # ── Check 1: Open position limit ──────────────────────────────────────────
    if open_positions >= settings.max_open_positions:
        reason = (
            f"Portfolio gate BLOCKED: {open_positions} open positions ≥ "
            f"limit {settings.max_open_positions}. No new trades."
        )
        logger.warning(reason)
        return False, reason

    # ── Check 2: Total exposure cap ────────────────────────────────────────────
    # 15% total exposure cap regardless of max_position_pct setting
    _MAX_TOTAL_EXPOSURE = 0.15
    if total_exposure_pct > _MAX_TOTAL_EXPOSURE:
        reason = (
            f"Portfolio gate BLOCKED: Total exposure {total_exposure_pct:.1%} > "
            f"cap {_MAX_TOTAL_EXPOSURE:.0%}. Reduce positions before adding new ones."
        )
        logger.warning(reason)
        return False, reason

    logger.debug(
        f"Portfolio gate PASSED: {open_positions} positions, "
        f"exposure={total_exposure_pct:.1%}"
    )
    return True, "Portfolio within limits."


def check_circuit_breaker(daily_pnl_pct: float) -> bool:
    """
    Tier 3: Activate the circuit breaker if daily losses exceed the limit.

    If the daily PnL percentage drops to or below -DAILY_LOSS_LIMIT_PCT,
    the circuit breaker activates and persists to SQLite. The agent will
    not trade again until reset_circuit_breaker() is called (which happens
    at the start of each new trading day via main.py's daily reset logic).

    Args:
        daily_pnl_pct: Today's PnL as a fraction (e.g. -0.05 = -5% today).

    Returns:
        True if circuit breaker was ACTIVATED (trading should halt).
        False if within acceptable daily loss range.

    Example:
        >>> halted = check_circuit_breaker(-0.09)
        >>> if halted:
        ...     logger.critical("Circuit breaker activated!")
    """
    from backend.memory.store import get_state, set_state

    if daily_pnl_pct <= -settings.daily_loss_limit_pct:
        now = datetime.now(UTC)
        recovery_time = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        set_state("circuit_breaker_active", "true")
        set_state("circuit_breaker_recovery_time", recovery_time.isoformat())
        set_state("cb_recovery_day", "1")   # Start gradual recovery at Day 1

        logger.critical(
            f"CIRCUIT BREAKER ACTIVATED! Daily PnL={daily_pnl_pct:.2%} ≤ "
            f"limit={-settings.daily_loss_limit_pct:.2%}. "
            f"Trading halted until {recovery_time.isoformat()}. "
            f"Gradual recovery: 25%→50%→75%→100% over 4 days."
        )
        return True

    return False


def is_circuit_breaker_active() -> bool:
    """
    Check if the circuit breaker is currently active.

    Reads the circuit breaker state from SQLite and checks if the recovery
    time has passed. If recovery time has elapsed, automatically deactivates
    the circuit breaker.

    Returns:
        True if the circuit breaker is active and recovery time has not passed.
        False if inactive or if recovery time has already elapsed.

    Example:
        >>> if is_circuit_breaker_active():
        ...     logger.warning("Circuit breaker active — skipping cycle")
    """
    from backend.memory.store import get_state, set_state

    try:
        active_val = get_state("circuit_breaker_active")
        if active_val != "true":
            return False

        recovery_str = get_state("circuit_breaker_recovery_time")
        if not recovery_str:
            # No recovery time set — deactivate as a safety measure
            set_state("circuit_breaker_active", "false")
            return False

        recovery_time = datetime.fromisoformat(recovery_str)
        now = datetime.now(UTC)

        if now >= recovery_time:
            # Recovery period has passed — advance recovery day counter
            recovery_day_str = get_state("cb_recovery_day") or "0"
            recovery_day = int(recovery_day_str) + 1
            set_state("cb_recovery_day", str(recovery_day))
            set_state("circuit_breaker_active", "false")  # Allow trading but at reduced size
            # Set next day's recovery time
            next_day = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            set_state("circuit_breaker_recovery_time", next_day.isoformat())

            if recovery_day >= 4:
                logger.info("Circuit breaker full recovery complete — position sizing back to 100%.")
                reset_circuit_breaker()
            else:
                mult = {1: 0.25, 2: 0.50, 3: 0.75}.get(recovery_day, 1.0)
                logger.info(
                    f"Circuit breaker recovery day {recovery_day}: "
                    f"trading resumed at {mult:.0%} position size."
                )
            return False

        remaining = (recovery_time - now).total_seconds() / 3600
        logger.warning(
            f"Circuit breaker ACTIVE. {remaining:.1f}h until recovery at {recovery_str}"
        )
        return True

    except Exception as exc:
        logger.error(f"Error checking circuit breaker state: {exc}")
        return False


def reset_circuit_breaker() -> None:
    """
    Reset the circuit breaker to allow trading to resume.

    Called either manually by the operator or automatically by main.py
    at the start of each new trading day. Clears both the active flag
    and the recovery time from SQLite.

    Example:
        >>> reset_circuit_breaker()
        >>> logger.info("Circuit breaker cleared — trading can resume")
    """
    from backend.memory.store import set_state

    set_state("circuit_breaker_active", "false")
    set_state("circuit_breaker_recovery_time", "")
    logger.info("Circuit breaker RESET — trading is now permitted.")
