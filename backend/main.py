"""
axion-trader main entry point — agent orchestration and API server lifecycle.

This module starts and coordinates all components of the trading system:

1. **Database initialisation** — creates SQLite schema on first run
2. **FastAPI server** — starts in a daemon thread on port 8000
3. **Shock guard** — asyncio task monitoring WebSocket price for flash crashes
4. **Three agent loops**:
   - Fast loop (every 15 min): rule engine only, immediate execution if confident
   - Standard loop (every 60 min): full cycle with Gemini + rules + all risk checks
   - Trend loop (every 4 hours): refresh 4h regime analysis

CLI flags:
  --agent-only    Run agent loops without starting the API server
  --api-only      Start only the API server without agent loops
  --paper         Force paper mode (overrides .env TRADING_MODE)
  --live          Force live mode (overrides .env TRADING_MODE)

Full standard cycle sequence:
  1. Fetch OHLCV (15m, 1h, 4h)
  2. Compute indicators for each timeframe
  3. Get sentiment (cached 10min)
  4. Score confluence
  5. Skip if confluence.passes_threshold == False
  6. Detect market regime
  7. Build reflection context from past trades
  8. Call Gemini → GeminiDecision
  9. Evaluate rules → RuleDecision
  10. Aggregate → FinalDecision
  11. Hold → save decision, continue
  12. Check Tier 2 + Tier 3 risk
  13. Check Tier 1 risk → TradeApproval
  14. Execute via trader.py → TradeResult
  15. Save all records to SQLite
  16. Save portfolio snapshot

Dependencies: asyncio, threading, argparse, uvicorn, rich, loguru
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from loguru import logger
from rich import print as rprint
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from backend.config.settings import settings
from backend.memory.store import init_db, set_state


# ── Logging setup ──────────────────────────────────────────────────────────────

class _InterceptHandler(logging.Handler):
    """Route all standard-library logging (uvicorn, fastapi) through loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = str(record.levelno)
        frame, depth = sys._getframe(6), 6
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back  # type: ignore[assignment]
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def setup_logging() -> None:
    """
    Configure loguru sinks: stdout (already default) + rotating file.

    File: logs/axion-trader.log
      - Rotates at 10 MB, retains 7 days, gzip-compressed on rotation.
      - Captures DEBUG and above from every module including uvicorn/fastapi.

    Uvicorn and FastAPI use standard library logging; we install an intercept
    handler so their output is funnelled through loguru and lands in the file.
    """
    log_path = Path(__file__).parent.parent / "logs" / "axion-trader.log"
    log_path.parent.mkdir(exist_ok=True)

    logger.add(
        str(log_path),
        level="DEBUG",
        rotation="10 MB",
        retention="7 days",
        compression="gz",
        enqueue=True,  # Async-safe writes from multiple threads
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | "
            "{name}:{function}:{line} - {message}"
        ),
    )

    # Redirect uvicorn / fastapi / httpx logs into loguru
    intercept = _InterceptHandler()
    logging.basicConfig(handlers=[intercept], level=0, force=True)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "fastapi", "httpx"):
        lg = logging.getLogger(name)
        lg.handlers = [intercept]
        lg.propagate = False

    logger.info(f"File logging active → {log_path}")

# ── Cycle intervals — read from settings so they can be overridden via .env ────
_FAST_LOOP_INTERVAL     = settings.fast_loop_minutes     * 60
_STANDARD_LOOP_INTERVAL = settings.standard_loop_minutes * 60
_TREND_LOOP_INTERVAL    = settings.trend_loop_minutes    * 60

# ── Global state ──────────────────────────────────────────────────────────────
console = Console()
_agent_running = True
_tasks: list[asyncio.Task[None]] = []


def print_startup_banner() -> None:
    """
    Print the rich startup banner to the terminal.

    Shows agent configuration, trading pair, mode, and key thresholds.
    Provides a clear visual confirmation that settings loaded correctly.

    Example:
        >>> print_startup_banner()
    """
    mode_colour = "yellow" if settings.is_paper_mode else "green"
    mode_label = "[PAPER]" if settings.is_paper_mode else "[LIVE]"

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim")
    table.add_column("Value", style="bold")

    table.add_row("Mode", f"[{mode_colour}]{mode_label}[/{mode_colour}]")
    table.add_row("Trading Pair", settings.trading_pair)
    table.add_row("Confidence Threshold", f"{settings.confidence_threshold:.0%}")
    table.add_row("Max Position", f"{settings.max_position_pct:.0%} of portfolio")
    table.add_row("Daily Loss Limit", f"{settings.daily_loss_limit_pct:.0%}")
    table.add_row("Max Open Positions", str(settings.max_open_positions))
    table.add_row("API Server", f"http://{settings.api_host}:{settings.api_port}")
    table.add_row("Database", str(settings.db_path))
    table.add_row("Fast Loop", "Every 15 minutes")
    table.add_row("Standard Loop", "Every 60 minutes")
    table.add_row("Trend Loop", "Every 4 hours")

    panel = Panel(
        table,
        title="[bold cyan]axion-trader v0.1.0[/bold cyan]",
        subtitle="[dim]Autonomous AI Trading Agent[/dim]",
        border_style="cyan",
        padding=(1, 2),
    )
    rprint(panel)


async def _check_stops_and_tp(current_price: float) -> None:
    """
    Check all open trades against their stop-loss and take-profit levels.

    Called on every fast loop tick. If current price crosses a stop or TP
    for any open position, places the closing market order immediately and
    updates the trade record in SQLite.

    Args:
        current_price: Latest market price from the fast loop snapshot.
    """
    from backend.memory.store import get_recent_trades, update_trade_exit
    from backend.execution.trader import place_order
    from datetime import UTC, datetime

    open_trades = [t for t in get_recent_trades(limit=50) if t.get("status") == "open"]
    if not open_trades:
        return

    now_ts = datetime.now(UTC).isoformat()

    for trade in open_trades:
        order_id = trade["order_id"]
        action = trade["action"]         # 'buy' or 'sell'
        volume = trade.get("volume", 0.0)
        entry_price = trade.get("entry_price", 0.0)
        stop_price = trade.get("stop_price", 0.0)
        tp_price = trade.get("take_profit_price", 0.0)

        if stop_price <= 0 or volume <= 0:
            continue  # Trade missing risk levels — skip

        triggered = None
        if action == "buy":
            if current_price <= stop_price:
                triggered = "STOP_LOSS"
            elif tp_price > 0 and current_price >= tp_price:
                triggered = "TAKE_PROFIT"
        else:  # sell / short
            if current_price >= stop_price:
                triggered = "STOP_LOSS"
            elif tp_price > 0 and current_price <= tp_price:
                triggered = "TAKE_PROFIT"

        if not triggered:
            continue

        close_action = "sell" if action == "buy" else "buy"
        logger.warning(
            f"{triggered} HIT | order={order_id} | action={action} | "
            f"entry=${entry_price:.2f} | stop=${stop_price:.2f} | "
            f"tp=${tp_price:.2f} | current=${current_price:.2f} → placing {close_action}"
        )

        try:
            close_result = place_order(close_action, volume, settings.trading_pair)

            if close_result.success:
                exit_px = current_price
                if action == "buy":
                    pnl_usd = (exit_px - entry_price) * volume
                else:
                    pnl_usd = (entry_price - exit_px) * volume
                pnl_pct = (pnl_usd / (entry_price * volume)) * 100 if entry_price > 0 else 0.0

                update_trade_exit(
                    order_id=order_id,
                    exit_price=exit_px,
                    pnl_usd=round(pnl_usd, 4),
                    pnl_pct=round(pnl_pct, 4),
                    closed_at=now_ts,
                )
                logger.info(
                    f"{triggered} closed | order={order_id} | "
                    f"PnL=${pnl_usd:+.2f} ({pnl_pct:+.2f}%)"
                )
            else:
                logger.error(f"{triggered} close FAILED for {order_id}: {close_result.error}")

        except Exception as exc:
            logger.error(f"Error closing {triggered} for {order_id}: {exc}")


async def run_fast_loop() -> None:
    """
    Fast loop — runs every 15 minutes, rule engine only.

    Fetches the current ticker price, computes indicators on the 15m timeframe,
    and evaluates the deterministic rule engine. If a rule fires with confidence
    >= 0.82, the agent executes the trade immediately without waiting for the
    hourly Gemini cycle.

    This loop prioritises speed and determinism over analytical depth,
    acting as a hair-trigger for high-confidence rule-based setups.

    Example:
        >>> asyncio.create_task(run_fast_loop())
    """
    logger.info("Fast loop started (15-minute rule-engine cycle)")

    while _agent_running:
        cycle_start = time.time()

        try:
            from backend.data.fetcher import fetch_ohlcv, fetch_ticker
            from backend.indicators.engine import compute_indicators
            from backend.indicators.regime import MarketRegime
            from backend.brain.rules import evaluate
            from backend.memory.store import get_state

            # Check circuit breaker before doing anything
            cb_active = get_state("circuit_breaker_active") == "true"
            if cb_active:
                logger.debug("Fast loop skipped: circuit breaker active")
                await asyncio.sleep(_FAST_LOOP_INTERVAL)
                continue

            # Fetch 15m OHLCV and compute indicators
            df_15m = fetch_ohlcv(settings.trading_pair, 15, 200)
            snap_15m = compute_indicators(df_15m)

            # Read last known regime from state for rule engine context
            regime_str = get_state("current_regime") or "RANGING"
            try:
                regime = MarketRegime(regime_str)
            except ValueError:
                regime = MarketRegime.RANGING

            # Update last known price in state
            set_state("last_price", str(snap_15m.current_price))

            # ── Stop-loss / take-profit enforcement ───────────────────────────
            # Must run every tick — this is what actually protects capital
            await _check_stops_and_tp(snap_15m.current_price)

            # Evaluate rule engine (fast — no API call)
            rule_decision = evaluate(snap_15m, regime)

            # Record every fast loop evaluation for later review
            from backend.memory.store import save_decision as _save_fast_decision
            _save_fast_decision({
                "timestamp": datetime.now(UTC).isoformat(),
                "pair": settings.trading_pair,
                "timeframe": 15,
                "rsi": snap_15m.rsi,
                "macd_cross": snap_15m.macd_cross_direction,
                "bb_position": _get_bb_position(snap_15m.bb_pct_b),
                "rule_action": rule_decision.action,
                "rule_confidence": rule_decision.confidence,
                "rule_triggered": rule_decision.triggered_rule,
                "final_action": rule_decision.action,
                "final_confidence": rule_decision.confidence,
                "consensus_reached": False,
                "approved_by_risk": False,
            })

            # Fast execution threshold: only act on very high confidence rules
            _FAST_LOOP_THRESHOLD = 0.82
            if rule_decision.confidence >= _FAST_LOOP_THRESHOLD and rule_decision.action != "hold":
                logger.info(
                    f"Fast loop HIGH-CONFIDENCE rule: {rule_decision.triggered_rule} "
                    f"→ {rule_decision.action} @ {rule_decision.confidence:.2f}"
                )

                # Run risk checks before executing
                from backend.risk.guard import approve_trade, check_portfolio, is_circuit_breaker_active
                from backend.execution.trader import place_order
                from backend.data.fetcher import fetch_balance, fetch_open_orders

                if not is_circuit_breaker_active():
                    # Quick portfolio check
                    from backend.memory.store import get_recent_trades as _get_trades_fast
                    open_count = len([t for t in _get_trades_fast(limit=50) if t.get("status") == "open"])
                    portfolio_ok, portfolio_msg = check_portfolio(open_count, open_count * settings.max_position_pct)

                    if portfolio_ok:
                        # Fetch balance for position sizing
                        try:
                            balances = fetch_balance()
                            usd_balance = balances.get("USD", balances.get("ZUSD", 10000.0))
                        except Exception:
                            usd_balance = 10000.0  # Fallback for paper mode

                        approval = approve_trade(
                            action=rule_decision.action,
                            confidence=rule_decision.confidence,
                            entry_price=snap_15m.current_price,
                            portfolio_value=usd_balance,
                            atr=snap_15m.atr,
                        )

                        if approval.approved:
                            volume = round(approval.position_size_usd / snap_15m.current_price, 6)
                            trade_result = place_order(rule_decision.action, volume, settings.trading_pair)

                            # Save stop/TP — trader.py initialises them to 0
                            if trade_result.success:
                                try:
                                    from backend.memory import store as _ms
                                    _conn = _ms._get_connection()
                                    _conn.execute(
                                        "UPDATE trades SET stop_price=?, take_profit_price=? WHERE order_id=?",
                                        (approval.stop_price, approval.take_profit_price, trade_result.order_id),
                                    )
                                    _conn.commit()
                                    _conn.close()
                                except Exception as _e:
                                    logger.warning(f"Fast loop: could not save stop/TP: {_e}")

                            logger.info(
                                f"Fast loop trade executed: {trade_result.action} "
                                f"{trade_result.volume} {settings.trading_pair} "
                                f"@ ${trade_result.price:,.2f} | "
                                f"stop=${approval.stop_price:,.2f} tp=${approval.take_profit_price:,.2f} "
                                f"(order_id={trade_result.order_id})"
                            )

        except Exception as exc:
            logger.warning(f"Fast loop error: {exc}")

        # Sleep for remainder of interval
        elapsed = time.time() - cycle_start
        sleep_time = max(0, _FAST_LOOP_INTERVAL - elapsed)
        await asyncio.sleep(sleep_time)


async def _snapshot_portfolio(cycle_ts: str) -> None:
    """
    Save a portfolio value snapshot to SQLite.

    Called at the end of every standard loop cycle regardless of whether a
    trade executed. This ensures portfolio_snapshots has continuous data for
    Sharpe ratio, drawdown, and metrics calculations — even during HOLD periods.
    """
    try:
        from backend.data.fetcher import fetch_balance
        from backend.memory.store import get_recent_trades, save_portfolio_snapshot
        from backend.memory.store import get_state as _get_state

        try:
            balances = fetch_balance()
            usd_bal = balances.get("USD", balances.get("ZUSD", 10000.0))
            btc_bal = balances.get("XBT", balances.get("XXBT", 0.0))
        except Exception:
            usd_bal = 10000.0
            btc_bal = 0.0

        last_price_str = _get_state("last_price") or "0"
        try:
            last_price = float(last_price_str)
        except ValueError:
            last_price = 0.0

        portfolio_total = usd_bal + btc_bal * last_price
        open_count = len([t for t in get_recent_trades(limit=50) if t.get("status") == "open"])

        save_portfolio_snapshot({
            "timestamp": cycle_ts,
            "total_value_usd": portfolio_total,
            "btc_balance": btc_bal,
            "usd_balance": usd_bal,
            "open_positions_count": open_count,
            "daily_pnl_usd": 0.0,
            "daily_pnl_pct": 0.0,
        })
        logger.debug(f"Portfolio snapshot: ${portfolio_total:,.2f} ({open_count} open positions)")
    except Exception as exc:
        logger.warning(f"Portfolio snapshot failed: {exc}")


async def run_standard_loop() -> None:
    """
    Standard loop — runs every 60 minutes, full AI decision cycle.

    Executes the complete trading pipeline:
    1. Multi-timeframe OHLCV fetch
    2. Indicator computation
    3. Sentiment analysis
    4. Confluence scoring
    5. Regime detection
    6. Gemini LLM decision
    7. Rule engine evaluation
    8. Consensus aggregation
    9. Three-tier risk management
    10. Order execution
    11. SQLite persistence

    This is the primary trading loop. The fast loop acts as a supplement
    for immediate high-confidence rule triggers.

    Example:
        >>> asyncio.create_task(run_standard_loop())
    """
    logger.info("Standard loop started (60-minute full AI cycle)")

    while _agent_running:
        cycle_start = time.time()
        cycle_ts = datetime.now(UTC).isoformat()

        # Set next cycle timestamp for dashboard countdown
        next_cycle_ts = datetime.fromtimestamp(
            cycle_start + _STANDARD_LOOP_INTERVAL, tz=UTC
        ).isoformat()
        set_state("next_cycle_timestamp", next_cycle_ts)
        set_state("agent_status", "running")

        try:
            await _run_full_cycle(cycle_ts)
        except Exception as exc:
            logger.error(f"Standard loop cycle error: {exc}")
        finally:
            # Always snapshot portfolio at end of every cycle — even on HOLD.
            # Without this, Sharpe/drawdown metrics have no data on non-trading days.
            await _snapshot_portfolio(cycle_ts)

        elapsed = time.time() - cycle_start
        sleep_time = max(0, _STANDARD_LOOP_INTERVAL - elapsed)
        logger.info(f"Standard loop cycle done in {elapsed:.1f}s. Next cycle in {sleep_time/60:.1f}min")
        await asyncio.sleep(sleep_time)


async def _run_full_cycle(cycle_ts: str) -> None:
    """
    Execute one complete trading cycle.

    This is the core decision pipeline extracted from the standard loop
    for clarity and testability.

    Args:
        cycle_ts: ISO 8601 timestamp of the cycle start for record-keeping.

    Example:
        >>> await _run_full_cycle('2024-01-01T00:00:00+00:00')
    """
    from backend.brain.aggregator import aggregate
    from backend.brain.gemini import GeminiDecision, MarketSnapshot, get_decision
    from backend.brain.reflection import get_reflection_context
    from backend.brain.rules import evaluate
    from backend.data.fetcher import fetch_balance, fetch_ohlcv, fetch_open_orders
    from backend.data.sentiment import get_sentiment_snapshot
    from backend.execution.trader import place_order
    from backend.indicators.confluence import score as confluence_score
    from backend.indicators.engine import compute_indicators
    from backend.indicators.regime import detect_regime
    from backend.memory.store import save_decision, save_portfolio_snapshot, save_trade
    from backend.risk.guard import (
        approve_trade,
        check_circuit_breaker,
        check_portfolio,
        is_circuit_breaker_active,
    )

    logger.info(f"=== Standard cycle starting at {cycle_ts} ===")

    # ── Step 1: Check circuit breaker ────────────────────────────────────────
    if is_circuit_breaker_active():
        logger.warning("Standard cycle SKIPPED: circuit breaker active")
        save_decision({
            "timestamp": cycle_ts,
            "pair": settings.trading_pair,
            "final_action": "hold",
            "final_confidence": 0.0,
            "consensus_reached": False,
            "approved_by_risk": False,
            "risk_rejection_reason": "Circuit breaker active — trading halted",
        })
        return

    # ── Step 2: Fetch OHLCV for all timeframes ───────────────────────────────
    logger.info("Fetching OHLCV data (15m, 1h, 4h)…")
    df_15m = fetch_ohlcv(settings.trading_pair, 15, 200)
    df_1h = fetch_ohlcv(settings.trading_pair, 60, 200)
    df_4h = fetch_ohlcv(settings.trading_pair, 240, 200)

    # ── Step 3: Compute indicators for each timeframe ────────────────────────
    snap_15m = compute_indicators(df_15m)
    snap_1h = compute_indicators(df_1h)
    snap_4h = compute_indicators(df_4h)

    # Use 1h snapshot as primary indicator set
    primary_snap = snap_1h
    logger.info(f"Indicators: RSI={primary_snap.rsi:.1f}, EMA9={primary_snap.ema_fast:.0f}, VWAP={primary_snap.vwap:.0f}")

    # Update last price in state
    set_state("last_price", str(primary_snap.current_price))

    # ── Step 4: Get market sentiment (cached 10min) ───────────────────────────
    logger.info("Fetching market sentiment…")
    sentiment = get_sentiment_snapshot()
    logger.info(f"Sentiment: F&G={sentiment.fear_greed_value} ({sentiment.fear_greed_classification}), news={sentiment.overall_news_sentiment}")

    # ── Step 5: Score confluence ───────────────────────────────────────────────
    confluence = confluence_score(primary_snap, sentiment)
    logger.info(f"Confluence: {confluence.score}/8 ({confluence.dominant_direction}), passes={confluence.passes_threshold}")

    # ── Step 6: Check confluence threshold ───────────────────────────────────
    if not confluence.passes_threshold:
        logger.info(f"Insufficient confluence ({confluence.score}/8) — skipping cycle")
        save_decision({
            "timestamp": cycle_ts,
            "pair": settings.trading_pair,
            "rsi": primary_snap.rsi,
            "macd_cross": primary_snap.macd_cross_direction,
            "bb_position": _get_bb_position(primary_snap.bb_pct_b),
            "confluence_score": confluence.score,
            "confluence_breakdown": json.dumps(confluence.signal_breakdown),
            "final_action": "hold",
            "final_confidence": 0.0,
            "consensus_reached": False,
            "approved_by_risk": False,
            "risk_rejection_reason": f"Insufficient confluence: {confluence.score}/8",
        })
        return

    # ── Step 7: Detect market regime ──────────────────────────────────────────
    logger.info("Detecting market regime…")
    regime_ctx = detect_regime(df_1h, df_4h)
    set_state("current_regime", regime_ctx.regime.value)
    logger.info(f"Regime: {regime_ctx.regime.value} (ADX={regime_ctx.adx_value:.1f})")

    # ── Step 8: Get reflection context ───────────────────────────────────────
    reflection = get_reflection_context(limit=10)

    # ── Step 9: Gemini LLM decision ───────────────────────────────────────────
    logger.info("Calling Gemini for trading decision…")
    news_headlines = [n.title for n in sentiment.news_items[:3]]

    market_snapshot = MarketSnapshot(
        pair=settings.trading_pair,
        current_price=primary_snap.current_price,
        rsi=primary_snap.rsi,
        macd_cross=primary_snap.macd_cross_direction,
        macd_histogram=primary_snap.macd_histogram,
        bb_pct_b=primary_snap.bb_pct_b,
        bb_upper=primary_snap.bb_upper,
        bb_lower=primary_snap.bb_lower,
        vwap=primary_snap.vwap,
        ema_fast=primary_snap.ema_fast,
        ema_slow=primary_snap.ema_slow,
        ema_cross=primary_snap.ema_cross,
        atr=primary_snap.atr,
        adx=primary_snap.adx,
        confluence_score=confluence.score,
        confluence_direction=confluence.dominant_direction,
        signal_breakdown=confluence.signal_breakdown,
        regime=regime_ctx.regime.value,
        fear_greed_value=sentiment.fear_greed_value,
        fear_greed_label=sentiment.fear_greed_classification,
        news_sentiment=sentiment.overall_news_sentiment,
        top_headlines=news_headlines,
    )

    llm_decision = get_decision(market_snapshot, reflection)
    logger.info(f"Gemini: {llm_decision.action} ({llm_decision.confidence:.2f})")

    # ── Step 10: Rule engine evaluation ──────────────────────────────────────
    rule_decision = evaluate(primary_snap, regime_ctx.regime)
    logger.info(f"Rules: {rule_decision.action} ({rule_decision.confidence:.2f}) [{rule_decision.triggered_rule}]")

    # ── Step 11: Aggregate decisions ──────────────────────────────────────────
    final_decision = aggregate(llm_decision, rule_decision)
    logger.info(f"Final: {final_decision.action} (conf={final_decision.final_confidence:.2f}, consensus={final_decision.consensus_reached})")

    # ── Step 12: Hold → save and continue ─────────────────────────────────────
    if final_decision.action == "hold":
        logger.info("Final decision: HOLD — saving and continuing")
        save_decision({
            "timestamp": cycle_ts,
            "pair": settings.trading_pair,
            "rsi": primary_snap.rsi,
            "macd_cross": primary_snap.macd_cross_direction,
            "bb_position": _get_bb_position(primary_snap.bb_pct_b),
            "confluence_score": confluence.score,
            "confluence_breakdown": json.dumps(confluence.signal_breakdown),
            "llm_action": llm_decision.action,
            "llm_confidence": llm_decision.confidence,
            "llm_reasoning": llm_decision.reasoning,
            "rule_action": rule_decision.action,
            "rule_confidence": rule_decision.confidence,
            "rule_triggered": rule_decision.triggered_rule,
            "final_action": "hold",
            "final_confidence": 0.0,
            "consensus_reached": final_decision.consensus_reached,
            "approved_by_risk": False,
        })
        return

    # ── Step 13: Tier 2 portfolio check ───────────────────────────────────────
    # Fetch balance here once — reused at Step 15 (no duplicate call).
    # Use SQLite open trades as the authoritative position count and exposure
    # calculation. Kraken paper orders fill instantly and vanish from the
    # open-orders list, so fetch_open_orders() always returns 0 in paper mode.
    try:
        balances = fetch_balance()
        usd_balance = balances.get("USD", balances.get("ZUSD", 10000.0))
        btc_balance = balances.get("XBT", balances.get("XXBT", 0.0))
    except Exception:
        usd_balance = 10000.0
        btc_balance = 0.0

    portfolio_value = usd_balance + btc_balance * primary_snap.current_price

    from backend.memory.store import get_recent_trades as _get_trades
    all_open_trades = [t for t in _get_trades(limit=50) if t.get("status") == "open"]
    open_count = len(all_open_trades)

    # Real exposure: sum of (volume × entry_price) for each open trade / portfolio
    total_deployed_usd = sum(
        t.get("volume", 0.0) * t.get("entry_price", 0.0)
        for t in all_open_trades
    )
    total_exposure_pct = total_deployed_usd / portfolio_value if portfolio_value > 0 else 0.0

    logger.debug(
        f"Portfolio exposure: {open_count} open trades, "
        f"${total_deployed_usd:.2f} deployed / ${portfolio_value:.2f} total = "
        f"{total_exposure_pct:.1%}"
    )

    portfolio_ok, portfolio_msg = check_portfolio(open_count, total_exposure_pct)
    if not portfolio_ok:
        logger.warning(f"Portfolio gate blocked: {portfolio_msg}")
        save_decision({
            "timestamp": cycle_ts, "pair": settings.trading_pair,
            "rsi": primary_snap.rsi, "confluence_score": confluence.score,
            "confluence_breakdown": json.dumps(confluence.signal_breakdown),
            "llm_action": llm_decision.action, "llm_confidence": llm_decision.confidence,
            "llm_reasoning": llm_decision.reasoning,
            "rule_action": rule_decision.action, "rule_triggered": rule_decision.triggered_rule,
            "final_action": final_decision.action, "final_confidence": final_decision.final_confidence,
            "consensus_reached": final_decision.consensus_reached,
            "approved_by_risk": False, "risk_rejection_reason": portfolio_msg,
        })
        return

    # ── Step 14: Tier 3 circuit breaker check ────────────────────────────────
    from backend.memory.store import get_recent_trades
    recent_trades = get_recent_trades(limit=10)
    daily_pnl = sum((t.get("pnl_pct") or 0) for t in recent_trades if t.get("status") == "closed")

    if check_circuit_breaker(daily_pnl / 100):  # Convert pct to fraction
        logger.critical("Circuit breaker activated — halting trading")
        return

    # ── Step 15: Tier 1 per-trade check ──────────────────────────────────────
    # usd_balance already fetched at Step 13 — no duplicate API call needed

    approval = approve_trade(
        action=final_decision.action,
        confidence=final_decision.final_confidence,
        entry_price=primary_snap.current_price,
        portfolio_value=portfolio_value,
        atr=primary_snap.atr,
    )

    if not approval.approved:
        logger.warning(f"Tier 1 risk rejected: {approval.reason}")
        save_decision({
            "timestamp": cycle_ts, "pair": settings.trading_pair,
            "rsi": primary_snap.rsi, "confluence_score": confluence.score,
            "confluence_breakdown": json.dumps(confluence.signal_breakdown),
            "llm_action": llm_decision.action, "llm_confidence": llm_decision.confidence,
            "llm_reasoning": llm_decision.reasoning,
            "rule_action": rule_decision.action, "rule_triggered": rule_decision.triggered_rule,
            "final_action": final_decision.action, "final_confidence": final_decision.final_confidence,
            "consensus_reached": final_decision.consensus_reached,
            "approved_by_risk": False, "risk_rejection_reason": approval.reason,
        })
        return

    # ── Step 16: Execute trade ─────────────────────────────────────────────────
    volume = round(approval.position_size_usd / primary_snap.current_price, 6)
    logger.info(f"Executing {final_decision.action.upper()} order: {volume} {settings.trading_pair}")

    trade_result = place_order(final_decision.action, volume, settings.trading_pair)

    # ── Step 17: Save complete records ────────────────────────────────────────
    decision_record = {
        "timestamp": cycle_ts,
        "pair": settings.trading_pair,
        "rsi": primary_snap.rsi,
        "macd_cross": primary_snap.macd_cross_direction,
        "bb_position": _get_bb_position(primary_snap.bb_pct_b),
        "confluence_score": confluence.score,
        "confluence_breakdown": json.dumps(confluence.signal_breakdown),
        "llm_action": llm_decision.action,
        "llm_confidence": llm_decision.confidence,
        "llm_reasoning": llm_decision.reasoning,
        "rule_action": rule_decision.action,
        "rule_confidence": rule_decision.confidence,
        "rule_triggered": rule_decision.triggered_rule,
        "final_action": final_decision.action,
        "final_confidence": final_decision.final_confidence,
        "consensus_reached": final_decision.consensus_reached,
        "approved_by_risk": approval.approved,
    }
    save_decision(decision_record)

    # Update trade record with stop/TP and AI reasoning
    try:
        from backend.memory import store as mem_store
        conn = mem_store._get_connection()
        conn.execute(
            "UPDATE trades SET stop_price=?, take_profit_price=?, llm_reasoning=? WHERE order_id=?",
            (approval.stop_price, approval.take_profit_price, llm_decision.reasoning, trade_result.order_id),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning(f"Could not update trade stop/TP: {exc}")

    # ── Rich log output ───────────────────────────────────────────────────────
    # Note: portfolio snapshot is saved by run_standard_loop's finally block
    mode_label = "[PAPER]" if settings.is_paper_mode else "[LIVE]"
    action_colour = "green" if final_decision.action == "buy" else "red"

    console.print(
        f"\n[bold]{mode_label}[/bold] [{action_colour}]{final_decision.action.upper()}[/{action_colour}] "
        f"{volume} {settings.trading_pair} @ ${primary_snap.current_price:,.2f} | "
        f"Conf: {final_decision.final_confidence:.0%} | "
        f"Stop: ${approval.stop_price:,.2f} | TP: ${approval.take_profit_price:,.2f}"
    )


async def run_trend_loop() -> None:
    """
    Trend loop — runs every 4 hours, refreshes regime analysis from 4h chart.

    Updates the current market regime in the SQLite state table so both the
    fast loop rule engine and dashboard have fresh regime context between
    standard cycles.

    Example:
        >>> asyncio.create_task(run_trend_loop())
    """
    logger.info("Trend loop started (4-hour regime refresh)")

    while _agent_running:
        try:
            from backend.data.fetcher import fetch_ohlcv
            from backend.indicators.regime import detect_regime
            from backend.memory.store import get_state as _get_regime_state

            df_1h = fetch_ohlcv(settings.trading_pair, 60, 200)
            df_4h = fetch_ohlcv(settings.trading_pair, 240, 200)
            regime_ctx = detect_regime(df_1h, df_4h)

            prev_regime = _get_regime_state("current_regime")
            set_state("current_regime", regime_ctx.regime.value)

            # Log regime change to decisions table for historical review
            if prev_regime != regime_ctx.regime.value:
                from backend.memory.store import save_decision as _save_regime
                _save_regime({
                    "timestamp": datetime.now(UTC).isoformat(),
                    "pair": settings.trading_pair,
                    "timeframe": 240,
                    "final_action": "hold",
                    "final_confidence": 0.0,
                    "consensus_reached": False,
                    "approved_by_risk": False,
                    "risk_rejection_reason": f"Regime changed: {prev_regime} → {regime_ctx.regime.value}",
                })
                logger.info(f"Trend loop: regime changed {prev_regime} → {regime_ctx.regime.value}")
            else:
                logger.info(f"Trend loop: regime unchanged → {regime_ctx.regime.value}")

        except Exception as exc:
            logger.warning(f"Trend loop error: {exc}")

        await asyncio.sleep(_TREND_LOOP_INTERVAL)


def _get_bb_position(bb_pct_b: float) -> str:
    """
    Convert Bollinger %B value to human-readable position string.

    Args:
        bb_pct_b: Bollinger %B value (0=lower band, 1=upper band).

    Returns:
        'LOWER', 'UPPER', or 'MIDDLE'.

    Example:
        >>> _get_bb_position(0.02)
        'LOWER'
    """
    if bb_pct_b <= 0.1:
        return "LOWER"
    elif bb_pct_b >= 0.9:
        return "UPPER"
    return "MIDDLE"


def start_api_server() -> None:
    """
    Start the FastAPI server in a daemon thread.

    Runs uvicorn in a separate thread so it doesn't block the main
    asyncio event loop running the agent loops.

    Example:
        >>> start_api_server()  # Non-blocking; returns immediately
    """
    config = uvicorn.Config(
        app="backend.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level="warning",  # Suppress uvicorn's verbose access logs
        access_log=False,
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True, name="uvicorn-api")
    thread.start()
    logger.info(f"API server started on http://{settings.api_host}:{settings.api_port}")


async def run_agent(include_shock_guard: bool = True) -> None:
    """
    Start all agent asyncio tasks and await them.

    Args:
        include_shock_guard: Whether to start the WebSocket shock guard task.

    Example:
        >>> asyncio.run(run_agent())
    """
    global _tasks

    from backend.execution.shock_guard import shock_guard

    logger.info("Starting agent tasks…")

    task_list: list[asyncio.Task[None]] = [
        asyncio.create_task(run_standard_loop(), name="standard_loop"),
        asyncio.create_task(run_fast_loop(), name="fast_loop"),
        asyncio.create_task(run_trend_loop(), name="trend_loop"),
    ]

    if include_shock_guard:
        task_list.append(
            asyncio.create_task(shock_guard.run(), name="shock_guard")
        )

    _tasks = task_list

    try:
        await asyncio.gather(*task_list)
    except asyncio.CancelledError:
        logger.info("Agent tasks cancelled — shutting down")


def print_session_summary() -> None:
    """
    Print a rich session summary table on shutdown.

    Shows key metrics from this trading session: trades executed,
    PnL, win rate, and final portfolio value.

    Example:
        >>> print_session_summary()
    """
    try:
        from backend.memory.store import compute_metrics

        metrics = compute_metrics()

        table = Table(title="Session Summary", show_header=True, header_style="bold")
        table.add_column("Metric", style="dim")
        table.add_column("Value", style="bold")

        table.add_row("Total Trades", str(metrics["total_trades"]))
        table.add_row("Win Rate", f"{metrics['win_rate_pct']:.1f}%")
        table.add_row("Total PnL", f"${metrics['total_pnl_usd']:+,.2f}")
        table.add_row("Sharpe Ratio", f"{metrics['sharpe_ratio']:.2f}")
        table.add_row("Max Drawdown", f"{metrics['max_drawdown_pct']:.2f}%")
        table.add_row("Portfolio Value", f"${metrics['portfolio_value_usd']:,.2f}")

        console.print(table)
    except Exception as exc:
        logger.warning(f"Could not compute session summary: {exc}")


def handle_shutdown(sig: int, frame: object) -> None:
    """
    Handle SIGINT and SIGTERM for graceful shutdown.

    Cancels all running asyncio tasks and prints the session summary.

    Args:
        sig: Signal number.
        frame: Current stack frame (unused).

    Example:
        >>> signal.signal(signal.SIGINT, handle_shutdown)
    """
    global _agent_running
    logger.warning(f"Received signal {sig} — initiating graceful shutdown…")
    _agent_running = False

    for task in _tasks:
        task.cancel()

    print_session_summary()


def parse_args() -> argparse.Namespace:
    """
    Parse command-line arguments.

    Returns:
        Parsed argument namespace with flags: agent_only, api_only, paper, live.

    Example:
        >>> args = parse_args()
        >>> if args.paper:
        ...     os.environ['TRADING_MODE'] = 'paper'
    """
    parser = argparse.ArgumentParser(
        description="axion-trader — Autonomous AI Trading Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python backend/main.py              # Start full system (agent + API)
  python backend/main.py --paper      # Force paper mode
  python backend/main.py --agent-only # Run agent without API server
  python backend/main.py --api-only   # Run only the REST API
        """,
    )
    parser.add_argument(
        "--agent-only",
        action="store_true",
        help="Run agent loops without starting the API server",
    )
    parser.add_argument(
        "--api-only",
        action="store_true",
        help="Start only the API server without agent loops",
    )
    parser.add_argument(
        "--paper",
        action="store_true",
        help="Force paper mode (overrides TRADING_MODE in .env)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Force live mode (overrides TRADING_MODE in .env)",
    )
    return parser.parse_args()


def main() -> None:
    """
    Main entry point — parses args, validates configuration, and starts all systems.

    Startup sequence:
    1. Parse CLI arguments
    2. Apply mode overrides from CLI flags
    3. Initialise database
    4. Print startup banner
    5. Register signal handlers
    6. Start API server (unless --agent-only)
    7. Run agent asyncio loops (unless --api-only)

    Example:
        >>> main()  # Entry point called by PDM scripts
    """
    import os

    # Configure file logging first — before anything else emits log lines
    setup_logging()

    args = parse_args()

    # Apply CLI mode overrides
    if args.paper:
        os.environ["TRADING_MODE"] = "paper"
        logger.info("CLI override: forcing PAPER mode")
    elif args.live:
        os.environ["TRADING_MODE"] = "live"
        logger.info("CLI override: forcing LIVE mode")

    # Initialise database
    init_db()

    # Print startup banner
    print_startup_banner()

    # Register shutdown handlers
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # ── API-only mode ─────────────────────────────────────────────────────────
    if args.api_only:
        logger.info("Running in API-only mode (no agent loops)")
        import uvicorn
        uvicorn.run(
            "backend.api.app:app",
            host=settings.api_host,
            port=settings.api_port,
            log_level="info",
        )
        return

    # ── Start API server in background thread ─────────────────────────────────
    if not args.agent_only:
        start_api_server()

    # ── Run agent asyncio loops ───────────────────────────────────────────────
    try:
        asyncio.run(run_agent(include_shock_guard=not args.agent_only))
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shutting down")
        print_session_summary()


if __name__ == "__main__":
    main()
