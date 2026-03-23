"""
Order execution module — Kraken CLI order placement in paper and live modes.

This module is the single point of contact between the agent's trading decisions
and the actual (or simulated) Kraken exchange. All order submission goes through
place_order(), which routes to either paper or live mode based on settings.

Paper mode:
    Uses ``kraken --paper order add ...`` which simulates the trade with real
    market prices but no actual execution. Perfect for testing strategies.

Live mode:
    Uses ``kraken order add ...`` which submits real orders to the Kraken
    exchange using the trading API key credentials. Real money, real execution.

All trade results are immediately written to SQLite via memory.store to ensure
no executed trade is ever lost, even if the agent crashes afterward.

Role in system: Final execution layer. Called by main.py only after all three
risk tiers have approved the trade.

Dependencies: subprocess (stdlib), json (stdlib), pydantic, loguru, memory.store
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from typing import Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from backend.config.settings import settings

_KRAKEN_BIN = shutil.which("kraken") or "kraken"


class TradeResult(BaseModel):
    """
    Result of a submitted trade order.

    Attributes:
        order_id: Kraken-assigned order ID string.
        action: 'buy' or 'sell'.
        pair: Trading pair symbol.
        volume: Order volume in base currency (BTC for BTCUSD).
        price: Execution price (market orders use fill price; 0.0 if unavailable).
        mode: 'paper' or 'live'.
        timestamp: ISO 8601 UTC timestamp of order submission.
        success: True if order was accepted by Kraken.
        error: Error message if success=False, None otherwise.

    Example:
        >>> result = place_order('buy', 0.001, 'BTCUSD')
        >>> print(result.order_id, result.success)
    """

    model_config = ConfigDict(frozen=True)

    order_id: str
    action: Literal["buy", "sell"]
    pair: str
    volume: float
    price: float
    mode: Literal["paper", "live"]
    timestamp: str
    success: bool
    error: str | None = None


def place_order(
    action: Literal["buy", "sell"],
    volume: float,
    pair: str | None = None,
) -> TradeResult:
    """
    Submit a market order to Kraken (paper or live mode).

    Builds the appropriate Kraken CLI command based on the current trading mode,
    submits it as a subprocess call with a 30-second timeout, and parses the
    JSON response. The result is immediately saved to SQLite.

    Paper mode command:
        kraken --paper order add --pair {pair} --type {action}
               --ordertype market --volume {volume} --acknowledged

    Live mode command:
        kraken order add --pair {pair} --type {action}
               --ordertype market --volume {volume} --acknowledged

    Args:
        action: 'buy' or 'sell'.
        volume: Order volume in base currency (e.g. BTC amount for BTCUSD).
        pair: Trading pair symbol. Defaults to settings.trading_pair.

    Returns:
        TradeResult with order details and success/failure status.

    Example:
        >>> result = place_order('buy', 0.001)
        >>> if result.success:
        ...     print(f"Order {result.order_id} placed at ${result.price}")
    """
    if pair is None:
        pair = settings.trading_pair

    mode_label = "[PAPER]" if settings.is_paper_mode else "[LIVE]"
    logger.info(f"{mode_label} Placing {action.upper()} order: {volume} {pair}")

    # Build CLI command
    # Paper mode: kraken paper buy/sell <PAIR> <VOLUME> --type market -o json --yes
    # Live mode:  kraken order buy/sell <PAIR> <VOLUME> --type market -o json --yes
    if settings.is_paper_mode:
        cmd = [
            _KRAKEN_BIN, "paper", action,
            pair, str(volume),
            "--type", "market",
            "-o", "json",
            "--yes",
        ]
    else:
        cmd = [
            _KRAKEN_BIN, "order", action,
            pair, str(volume),
            "--type", "market",
            "-o", "json",
            "--yes",
        ]

    timestamp = datetime.now(UTC).isoformat()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() or f"Exit code {result.returncode}"
            logger.error(f"{mode_label} Order FAILED: {error_msg}")
            trade = TradeResult(
                order_id=f"failed_{timestamp}",
                action=action,
                pair=pair,
                volume=volume,
                price=0.0,
                mode="paper" if settings.is_paper_mode else "live",
                timestamp=timestamp,
                success=False,
                error=error_msg,
            )
            _save_trade_result(trade)
            return trade

        # Parse JSON response from Kraken CLI
        output = result.stdout.strip()
        order_id = _extract_order_id(output)
        fill_price = _extract_fill_price(output)

        trade = TradeResult(
            order_id=order_id,
            action=action,
            pair=pair,
            volume=volume,
            price=fill_price,
            mode="paper" if settings.is_paper_mode else "live",
            timestamp=timestamp,
            success=True,
            error=None,
        )

        logger.info(
            f"{mode_label} Order {order_id} ACCEPTED: {action.upper()} "
            f"{volume} {pair} @ ${fill_price:,.2f}"
        )

        _save_trade_result(trade)
        return trade

    except subprocess.TimeoutExpired:
        error_msg = "Kraken CLI timed out after 30 seconds"
        logger.error(f"{mode_label} Order TIMEOUT: {error_msg}")
        trade = TradeResult(
            order_id=f"timeout_{timestamp}",
            action=action,
            pair=pair,
            volume=volume,
            price=0.0,
            mode="paper" if settings.is_paper_mode else "live",
            timestamp=timestamp,
            success=False,
            error=error_msg,
        )
        _save_trade_result(trade)
        return trade

    except Exception as exc:
        error_msg = str(exc)
        logger.error(f"{mode_label} Order ERROR: {error_msg}")
        trade = TradeResult(
            order_id=f"error_{timestamp}",
            action=action,
            pair=pair,
            volume=volume,
            price=0.0,
            mode="paper" if settings.is_paper_mode else "live",
            timestamp=timestamp,
            success=False,
            error=error_msg,
        )
        _save_trade_result(trade)
        return trade


def close_position(order_id: str) -> TradeResult:
    """
    Close a specific open position by order ID.

    Calls ``kraken order cancel --orderid {order_id}`` to cancel the
    open order. For market positions without a GTC order, uses
    ``kraken order add`` with the opposite side.

    Args:
        order_id: The Kraken order ID to close/cancel.

    Returns:
        TradeResult indicating the close/cancel outcome.

    Example:
        >>> result = close_position('OQCLML-BW3P3-BUCMWW')
    """
    mode_label = "[PAPER]" if settings.is_paper_mode else "[LIVE]"
    logger.info(f"{mode_label} Closing position {order_id}")

    # Paper mode: kraken paper cancel <TXID> -o json --yes
    # Live mode:  kraken order cancel <TXID> -o json --yes
    if settings.is_paper_mode:
        cmd = [_KRAKEN_BIN, "paper", "cancel", order_id, "-o", "json", "--yes"]
    else:
        cmd = [_KRAKEN_BIN, "order", "cancel", order_id, "-o", "json", "--yes"]

    timestamp = datetime.now(UTC).isoformat()

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            error_msg = result.stderr.strip() or f"Cancel exit code {result.returncode}"
            logger.error(f"{mode_label} Close FAILED for {order_id}: {error_msg}")
            return TradeResult(
                order_id=order_id,
                action="sell",  # Default close direction
                pair=settings.trading_pair,
                volume=0.0,
                price=0.0,
                mode="paper" if settings.is_paper_mode else "live",
                timestamp=timestamp,
                success=False,
                error=error_msg,
            )

        logger.info(f"{mode_label} Position {order_id} closed successfully")
        return TradeResult(
            order_id=order_id,
            action="sell",
            pair=settings.trading_pair,
            volume=0.0,
            price=0.0,
            mode="paper" if settings.is_paper_mode else "live",
            timestamp=timestamp,
            success=True,
        )

    except Exception as exc:
        return TradeResult(
            order_id=order_id,
            action="sell",
            pair=settings.trading_pair,
            volume=0.0,
            price=0.0,
            mode="paper" if settings.is_paper_mode else "live",
            timestamp=timestamp,
            success=False,
            error=str(exc),
        )


def close_all_positions() -> list[TradeResult]:
    """
    Emergency close of all open positions.

    Fetches all open orders and cancels each one. Used by the shock guard
    when a 3% price drop in 5 minutes triggers emergency exit protocol.

    Returns:
        List of TradeResult objects, one per position close attempt.

    Example:
        >>> results = close_all_positions()
        >>> failed = [r for r in results if not r.success]
    """
    from backend.data.fetcher import fetch_open_orders

    mode_label = "[PAPER]" if settings.is_paper_mode else "[LIVE]"
    logger.critical(f"{mode_label} EMERGENCY: Closing ALL open positions")

    results: list[TradeResult] = []

    try:
        open_orders = fetch_open_orders()
        if not open_orders:
            logger.info(f"{mode_label} No open positions to close")
            return results

        for order in open_orders:
            order_id = order.get("order_id", "")
            if order_id:
                result = close_position(order_id)
                results.append(result)

    except Exception as exc:
        logger.error(f"Failed to fetch open orders during emergency close: {exc}")

    logger.critical(
        f"{mode_label} Emergency close complete: "
        f"{sum(1 for r in results if r.success)}/{len(results)} positions closed"
    )
    return results


def _extract_order_id(output: str) -> str:
    """
    Extract order ID from Kraken CLI JSON output.

    Args:
        output: Raw stdout from Kraken CLI order command.

    Returns:
        Order ID string, or a generated fallback ID if parsing fails.

    Example:
        >>> order_id = _extract_order_id('{"txid": ["OQCLML-BW3P3-BUCMWW"]}')
    """
    try:
        data = json.loads(output)
        # Kraken returns txids as a list
        if isinstance(data, dict):
            txids = data.get("txid", data.get("order_id", []))
            if isinstance(txids, list) and txids:
                return str(txids[0])
            elif isinstance(txids, str):
                return txids
    except (json.JSONDecodeError, KeyError):
        pass

    # Fallback: generate a timestamp-based ID
    return f"order_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"


def _extract_fill_price(output: str) -> float:
    """
    Extract fill price from Kraken CLI JSON output.

    Args:
        output: Raw stdout from Kraken CLI order command.

    Returns:
        Fill price as float, or 0.0 if not available (e.g. market order pending).

    Example:
        >>> price = _extract_fill_price('{"price": "67234.10"}')
    """
    try:
        data = json.loads(output)
        if isinstance(data, dict):
            price = data.get("price", data.get("avg_price", 0))
            if price:
                return float(price)
    except (json.JSONDecodeError, ValueError, KeyError):
        pass
    return 0.0


def _save_trade_result(trade: TradeResult) -> None:
    """
    Persist a trade result to SQLite.

    Called immediately after every order attempt so no trade is ever lost.
    Errors are logged but not re-raised — trade data loss is better than
    crashing the agent mid-execution.

    Args:
        trade: Completed TradeResult to save.

    Example:
        >>> _save_trade_result(trade_result)
    """
    try:
        from backend.memory.store import save_trade

        trade_record = {
            "order_id": trade.order_id,
            "timestamp": trade.timestamp,
            "pair": trade.pair,
            "action": trade.action,
            "volume": trade.volume,
            "entry_price": trade.price,
            "exit_price": None,
            "pnl_usd": None,
            "pnl_pct": None,
            "status": "open" if trade.success else "failed",
            "stop_price": 0.0,
            "take_profit_price": 0.0,
            "mode": trade.mode,
            "closed_at": None,
        }
        save_trade(trade_record)
    except Exception as exc:
        logger.error(f"Failed to save trade result to SQLite: {exc}")
