"""
Order execution module — limit-first with market fallback, micro-pullback entry,
and execution quality tracking.

Order flow:
  1. Compute limit price = signal_price ± micro_pullback_pct (0.3% by default)
  2. Place limit order via Kraken CLI
  3. Poll for fill every 3 seconds for up to limit_order_timeout_s (default 30s)
  4. If unfilled → cancel → place market order as fallback
  5. Record expected vs actual entry price in execution_quality table

Maker vs taker fee impact:
  Limit order (maker): 0.16% on Kraken
  Market order (taker): 0.26% on Kraken
  Round-trip saving: 0.20% per trade — meaningful at scale.

Micro-pullback benefit:
  Signal fires at $70,000. Limit set at $69,790 (0.3% below).
  Better entry price → tighter stop → better R:R.
  If price doesn't pull back, we miss the trade — that's fine,
  there will be others with better entries.

Dependencies: subprocess (stdlib), json (stdlib), time (stdlib), pydantic, loguru
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
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
        price: Execution price (0.0 if market order pending fill).
        signal_price: Price when the signal fired (before micro-pullback offset).
        order_type: 'limit' or 'market'.
        mode: 'paper' or 'live'.
        timestamp: ISO 8601 UTC timestamp of order submission.
        success: True if order was accepted by Kraken.
        error: Error message if success=False, None otherwise.
        slippage_pct: Actual entry vs signal price as percentage.

    Example:
        >>> result = place_order('buy', 0.001, signal_price=67000.0)
        >>> print(result.order_id, result.slippage_pct)
    """

    model_config = ConfigDict(frozen=True)

    order_id: str
    action: Literal["buy", "sell"]
    pair: str
    volume: float
    price: float
    signal_price: float = Field(default=0.0, description="Price when the signal fired.")
    order_type: Literal["limit", "market"] = "market"
    mode: Literal["paper", "live"]
    timestamp: str
    success: bool
    error: str | None = None
    slippage_pct: float = Field(default=0.0, description="Entry vs signal price deviation (%).")


def place_order(
    action: Literal["buy", "sell"],
    volume: float,
    pair: str | None = None,
    signal_price: float = 0.0,
) -> TradeResult:
    """
    Submit an order — limit first, market fallback.

    If settings.use_limit_orders is True:
      - Computes a micro-pullback limit price
      - Places a limit order
      - Polls for fill for up to settings.limit_order_timeout_s
      - Falls back to market order if unfilled

    If settings.use_limit_orders is False:
      - Places a market order directly (original behaviour)

    Args:
        action: 'buy' or 'sell'.
        volume: Order volume in base currency.
        pair: Trading pair. Defaults to settings.trading_pair.
        signal_price: Price when the signal fired (for slippage tracking).

    Returns:
        TradeResult with order details and execution quality metrics.

    Example:
        >>> result = place_order('buy', 0.001, signal_price=67000.0)
    """
    if pair is None:
        pair = settings.trading_pair

    if settings.use_limit_orders and signal_price > 0:
        return _place_limit_with_fallback(action, volume, pair, signal_price)

    return _place_market_order(action, volume, pair, signal_price)


def _compute_limit_price(action: Literal["buy", "sell"], signal_price: float) -> float:
    """
    Compute micro-pullback limit price.

    For buys:  limit = signal_price × (1 - pullback_pct)  → wait for dip
    For sells: limit = signal_price × (1 + pullback_pct)  → wait for bounce

    Args:
        action: 'buy' or 'sell'.
        signal_price: Current price when signal fired.

    Returns:
        Limit price rounded to 2 decimal places.
    """
    pct = settings.micro_pullback_pct
    if action == "buy":
        return round(signal_price * (1.0 - pct), 2)
    else:
        return round(signal_price * (1.0 + pct), 2)


def _place_limit_with_fallback(
    action: Literal["buy", "sell"],
    volume: float,
    pair: str,
    signal_price: float,
) -> TradeResult:
    """Place a limit order, poll for fill, fall back to market if timeout."""
    limit_price = _compute_limit_price(action, signal_price)
    mode_label  = "[PAPER]" if settings.is_paper_mode else "[LIVE]"

    logger.info(
        f"{mode_label} LIMIT {action.upper()} {volume} {pair} @ ${limit_price:,.2f} "
        f"(signal=${signal_price:,.2f}, pullback={settings.micro_pullback_pct:.2%})"
    )

    # Place limit order
    limit_result = _submit_kraken_order(action, volume, pair, "limit", limit_price)

    if not limit_result.success:
        logger.warning(f"{mode_label} Limit order failed, falling back to market: {limit_result.error}")
        return _place_market_order(action, volume, pair, signal_price)

    # Poll for fill
    timeout = settings.limit_order_timeout_s
    poll_interval = 3  # seconds
    elapsed = 0

    while elapsed < timeout:
        time.sleep(poll_interval)
        elapsed += poll_interval

        fill_status = _check_order_fill(limit_result.order_id)
        if fill_status == "filled":
            fill_price = _get_fill_price(limit_result.order_id) or limit_price
            slippage = _calc_slippage(signal_price, fill_price)
            result = TradeResult(
                order_id=limit_result.order_id,
                action=action,
                pair=pair,
                volume=volume,
                price=fill_price,
                signal_price=signal_price,
                order_type="limit",
                mode="paper" if settings.is_paper_mode else "live",
                timestamp=limit_result.timestamp,
                success=True,
                slippage_pct=slippage,
            )
            logger.info(
                f"{mode_label} Limit filled: {action.upper()} @ ${fill_price:,.2f} "
                f"(slippage={slippage:+.3f}%, signal=${signal_price:,.2f})"
            )
            _save_trade_result(result)
            _save_execution_quality(result)
            return result

        logger.debug(f"{mode_label} Limit order not yet filled ({elapsed}s/{timeout}s)...")

    # Timeout — cancel and fall back to market
    logger.warning(
        f"{mode_label} Limit order unfilled after {timeout}s → cancelling and using market order"
    )
    _cancel_order_silent(limit_result.order_id)
    return _place_market_order(action, volume, pair, signal_price)


def _place_market_order(
    action: Literal["buy", "sell"],
    volume: float,
    pair: str,
    signal_price: float = 0.0,
) -> TradeResult:
    """Place a market order (original behaviour, used as fallback)."""
    mode_label = "[PAPER]" if settings.is_paper_mode else "[LIVE]"
    logger.info(f"{mode_label} MARKET {action.upper()} {volume} {pair}")

    result = _submit_kraken_order(action, volume, pair, "market", None)

    slippage = _calc_slippage(signal_price, result.price) if signal_price > 0 else 0.0

    final = TradeResult(
        order_id=result.order_id,
        action=action,
        pair=pair,
        volume=volume,
        price=result.price,
        signal_price=signal_price,
        order_type="market",
        mode="paper" if settings.is_paper_mode else "live",
        timestamp=result.timestamp,
        success=result.success,
        error=result.error,
        slippage_pct=slippage,
    )
    _save_trade_result(final)
    if signal_price > 0 and final.success:
        _save_execution_quality(final)
    return final


def _submit_kraken_order(
    action: Literal["buy", "sell"],
    volume: float,
    pair: str,
    order_type: Literal["limit", "market"],
    limit_price: float | None,
) -> TradeResult:
    """Build and execute a Kraken CLI order command."""
    timestamp = datetime.now(UTC).isoformat()

    if settings.is_paper_mode:
        cmd = [_KRAKEN_BIN, "paper", action, pair, str(volume), "--type", order_type]
    else:
        cmd = [_KRAKEN_BIN, "order", action, pair, str(volume), "--type", order_type]

    if order_type == "limit" and limit_price is not None:
        cmd += ["--limit", str(limit_price)]

    cmd += ["-o", "json", "--yes"]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if proc.returncode != 0:
            err = proc.stderr.strip() or f"Exit code {proc.returncode}"
            logger.error(f"Kraken order FAILED: {err}")
            return TradeResult(
                order_id=f"failed_{timestamp}",
                action=action, pair=pair, volume=volume,
                price=0.0, signal_price=0.0,
                order_type=order_type,
                mode="paper" if settings.is_paper_mode else "live",
                timestamp=timestamp, success=False, error=err,
            )

        output    = proc.stdout.strip()
        order_id  = _extract_order_id(output)
        fill_price = _extract_fill_price(output)

        return TradeResult(
            order_id=order_id,
            action=action, pair=pair, volume=volume,
            price=fill_price, signal_price=0.0,
            order_type=order_type,
            mode="paper" if settings.is_paper_mode else "live",
            timestamp=timestamp, success=True,
        )

    except subprocess.TimeoutExpired:
        err = "Kraken CLI timed out after 30 seconds"
        return TradeResult(
            order_id=f"timeout_{timestamp}",
            action=action, pair=pair, volume=volume,
            price=0.0, signal_price=0.0,
            order_type=order_type,
            mode="paper" if settings.is_paper_mode else "live",
            timestamp=timestamp, success=False, error=err,
        )
    except Exception as exc:
        return TradeResult(
            order_id=f"error_{timestamp}",
            action=action, pair=pair, volume=volume,
            price=0.0, signal_price=0.0,
            order_type=order_type,
            mode="paper" if settings.is_paper_mode else "live",
            timestamp=timestamp, success=False, error=str(exc),
        )


def _check_order_fill(order_id: str) -> str:
    """Query Kraken CLI for fill status. Returns 'filled', 'open', or 'unknown'."""
    try:
        cmd = [_KRAKEN_BIN, "order", "status", order_id, "-o", "json"]
        if settings.is_paper_mode:
            cmd = [_KRAKEN_BIN, "paper", "status", order_id, "-o", "json"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            return "unknown"
        data = json.loads(proc.stdout.strip())
        status = str(data.get("status", "")).lower()
        if "closed" in status or "filled" in status:
            return "filled"
        return "open"
    except Exception:
        return "unknown"


def _get_fill_price(order_id: str) -> float | None:
    """Retrieve fill price for a completed order."""
    try:
        cmd = [_KRAKEN_BIN, "order", "status", order_id, "-o", "json"]
        if settings.is_paper_mode:
            cmd = [_KRAKEN_BIN, "paper", "status", order_id, "-o", "json"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            data = json.loads(proc.stdout.strip())
            price = data.get("price") or data.get("avg_price")
            if price:
                return float(price)
    except Exception:
        pass
    return None


def _cancel_order_silent(order_id: str) -> None:
    """Cancel an order, logging errors but not raising."""
    try:
        cmd = [_KRAKEN_BIN, "paper" if settings.is_paper_mode else "order",
               "cancel", order_id, "--yes"]
        subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except Exception as exc:
        logger.warning(f"Failed to cancel order {order_id}: {exc}")


def _calc_slippage(signal_price: float, fill_price: float) -> float:
    """Compute signed slippage as percentage: (fill - signal) / signal × 100."""
    if signal_price <= 0 or fill_price <= 0:
        return 0.0
    return round((fill_price - signal_price) / signal_price * 100, 4)


def close_position(order_id: str) -> TradeResult:
    """Close a specific open position by order ID."""
    mode_label = "[PAPER]" if settings.is_paper_mode else "[LIVE]"
    logger.info(f"{mode_label} Closing position {order_id}")

    cmd = (
        [_KRAKEN_BIN, "paper", "cancel", order_id, "-o", "json", "--yes"]
        if settings.is_paper_mode
        else [_KRAKEN_BIN, "order", "cancel", order_id, "-o", "json", "--yes"]
    )
    timestamp = datetime.now(UTC).isoformat()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            err = proc.stderr.strip() or f"Cancel exit code {proc.returncode}"
            return TradeResult(
                order_id=order_id, action="sell", pair=settings.trading_pair,
                volume=0.0, price=0.0,
                mode="paper" if settings.is_paper_mode else "live",
                timestamp=timestamp, success=False, error=err,
            )
        logger.info(f"{mode_label} Position {order_id} closed")
        return TradeResult(
            order_id=order_id, action="sell", pair=settings.trading_pair,
            volume=0.0, price=0.0,
            mode="paper" if settings.is_paper_mode else "live",
            timestamp=timestamp, success=True,
        )
    except Exception as exc:
        return TradeResult(
            order_id=order_id, action="sell", pair=settings.trading_pair,
            volume=0.0, price=0.0,
            mode="paper" if settings.is_paper_mode else "live",
            timestamp=timestamp, success=False, error=str(exc),
        )


def close_all_positions() -> list[TradeResult]:
    """Emergency close of all open positions."""
    from backend.data.fetcher import fetch_open_orders
    mode_label = "[PAPER]" if settings.is_paper_mode else "[LIVE]"
    logger.critical(f"{mode_label} EMERGENCY: Closing ALL open positions")
    results: list[TradeResult] = []
    try:
        open_orders = fetch_open_orders()
        for order in open_orders:
            order_id = order.get("order_id", "")
            if order_id:
                results.append(close_position(order_id))
    except Exception as exc:
        logger.error(f"Failed to fetch open orders during emergency close: {exc}")
    logger.critical(
        f"{mode_label} Emergency close: {sum(1 for r in results if r.success)}/{len(results)} closed"
    )
    return results


# ── Parsing helpers ────────────────────────────────────────────────────────────

def _extract_order_id(output: str) -> str:
    try:
        data = json.loads(output)
        if isinstance(data, dict):
            txids = data.get("txid", data.get("order_id", []))
            if isinstance(txids, list) and txids:
                return str(txids[0])
            elif isinstance(txids, str):
                return txids
    except (json.JSONDecodeError, KeyError):
        pass
    return f"order_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"


def _extract_fill_price(output: str) -> float:
    try:
        data = json.loads(output)
        if isinstance(data, dict):
            price = data.get("price", data.get("avg_price", 0))
            if price:
                return float(price)
    except (json.JSONDecodeError, ValueError, KeyError):
        pass
    return 0.0


# ── Persistence helpers ────────────────────────────────────────────────────────

def _save_trade_result(trade: TradeResult) -> None:
    try:
        from backend.memory.store import save_trade
        save_trade({
            "order_id":         trade.order_id,
            "timestamp":        trade.timestamp,
            "pair":             trade.pair,
            "action":           trade.action,
            "volume":           trade.volume,
            "entry_price":      trade.price,
            "exit_price":       None,
            "pnl_usd":          None,
            "pnl_pct":          None,
            "status":           "open" if trade.success else "failed",
            "stop_price":       0.0,
            "take_profit_price": 0.0,
            "mode":             trade.mode,
            "closed_at":        None,
        })
    except Exception as exc:
        logger.error(f"Failed to save trade result: {exc}")


def _save_execution_quality(trade: TradeResult) -> None:
    try:
        from backend.memory.store import save_execution_quality
        save_execution_quality({
            "order_id":     trade.order_id,
            "timestamp":    trade.timestamp,
            "pair":         trade.pair,
            "action":       trade.action,
            "signal_price": trade.signal_price,
            "entry_price":  trade.price,
            "slippage_pct": trade.slippage_pct,
            "order_type":   trade.order_type,
        })
    except Exception as exc:
        logger.warning(f"Failed to save execution quality record: {exc}")
