"""
Shock guard — real-time emergency exit on rapid price drops + OFI tracking.

This module implements a WebSocket-based monitor that runs as a background
asyncio task and serves two purposes:

1. SHOCK DETECTION (original)
   Watches for sudden large price drops that require immediate position
   closure before the standard cycle can react.
   Trigger: (window_high - current_price) / window_high > 3% in 5 minutes.

2. ORDER FLOW IMBALANCE (OFI) accumulation
   Subscribes to the level-2 book channel alongside the ticker channel.
   On each book update, records the signed delta:
     delta = best_bid_qty - best_ask_qty
   Accumulates a rolling sum of signed deltas over the last N ticks to
   form an OFI score the confluence engine can query.

   OFI interpretation:
     positive OFI → buyers are more aggressive → bullish pressure
     negative OFI → sellers are more aggressive → bearish pressure
     near-zero OFI → balanced order flow → neutral

   Why OFI? Unlike price, OFI detects *intent* before price moves.
   A large positive OFI while price is flat often precedes an upward
   breakout, and vice-versa.

Architecture:
- Subscribes to Kraken WebSocket v2 ticker + book channels
- Maintains a deque of recent prices (shock detection)
- Maintains a deque of signed bid/ask deltas (OFI accumulation)
- Exposes `ofi_score` property: rolling sum of last N deltas, normalised
- Emergency exit: close_all_positions() + activate circuit breaker
- Auto-reconnects with exponential backoff (1s → 2s → 4s → 8s → max 60s)

Role in system: Runs as a concurrent asyncio task started in main.py.
Confluence engine calls `shock_guard.ofi_score` during each decision cycle.

Dependencies: websockets, asyncio, collections.deque, loguru
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import UTC, datetime
from typing import Any

import websockets
from loguru import logger

from backend.config.settings import settings

# Kraken WebSocket v2 endpoint (public, no authentication required for ticker)
_WS_URL = "wss://ws.kraken.com/v2"

# Emergency trigger: 3% drop from window high within the rolling window
_SHOCK_THRESHOLD_PCT = 0.03

# Rolling window duration for price monitoring (300 seconds = 5 minutes)
_WINDOW_SECONDS = 300

# Maximum deque size: assume 1 tick per second → 300 ticks = 5 minutes
_MAX_DEQUE_SIZE = 300

# OFI rolling window: accumulate over last N book updates
# At ~1 book update per second, 200 ticks ≈ 3 minutes of flow data
_OFI_WINDOW = 200

# Reconnect backoff configuration
_INITIAL_BACKOFF = 1.0  # seconds
_MAX_BACKOFF = 60.0     # seconds
_BACKOFF_MULTIPLIER = 2.0


class ShockGuard:
    """
    Real-time WebSocket price monitor with emergency exit + OFI accumulation.

    Maintains a rolling window of recent prices (shock detection) and a
    rolling deque of signed bid/ask deltas (OFI = Order Flow Imbalance).

    Attributes:
        is_running: True while the WebSocket connection is active.
        last_price: Most recently received price tick.
        last_update: ISO 8601 timestamp of last price update.
        emergency_triggered: True if the emergency exit has fired this session.
        ofi_score: Rolling sum of signed bid/ask deltas, normalised to [-1, 1].
                   Positive → buy pressure, negative → sell pressure.

    Example:
        >>> guard = ShockGuard()
        >>> asyncio.create_task(guard.run())
        >>> print(guard.ofi_score)   # -1.0 to +1.0
    """

    def __init__(self) -> None:
        """Initialise the shock guard with an empty price window and OFI deque."""
        # Deque of (unix_timestamp: float, price: float) tuples
        self._price_window: deque[tuple[float, float]] = deque(maxlen=_MAX_DEQUE_SIZE)
        # Deque of signed bid/ask deltas for OFI: positive = buy pressure
        self._ofi_deltas: deque[float] = deque(maxlen=_OFI_WINDOW)
        self._is_running: bool = False
        self._last_price: float = 0.0
        self._last_update: str = ""
        self._emergency_triggered: bool = False
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        """True while the WebSocket connection is active and receiving ticks."""
        return self._is_running

    @property
    def last_price(self) -> float:
        """Most recently received BTC/USD price tick."""
        return self._last_price

    @property
    def last_update(self) -> str:
        """ISO 8601 UTC timestamp of the last received price update."""
        return self._last_update

    @property
    def emergency_triggered(self) -> bool:
        """True if an emergency exit has been triggered this session."""
        return self._emergency_triggered

    @property
    def ofi_score(self) -> float:
        """
        Current Order Flow Imbalance score, normalised to [-1.0, 1.0].

        Computed as the mean of the last _OFI_WINDOW signed bid/ask deltas,
        where each delta = best_bid_qty - best_ask_qty at each book update.

        Returns:
            Float in [-1.0, 1.0].
            +1.0 → sustained aggressive buying pressure
            -1.0 → sustained aggressive selling pressure
             0.0 → balanced or no data yet

        Example:
            >>> score = guard.ofi_score
            >>> if score > 0.3:
            ...     print("Strong buy flow")
        """
        if not self._ofi_deltas:
            return 0.0
        raw = sum(self._ofi_deltas) / len(self._ofi_deltas)
        # Normalise: cap at ±100 BTC equivalent, map to [-1, 1]
        return max(-1.0, min(1.0, raw / 100.0))

    def _accumulate_ofi(self, data: dict[str, Any]) -> None:
        """
        Extract best bid/ask from a book snapshot/update and accumulate OFI.

        Kraken WebSocket v2 book message format (snapshot):
            {"channel": "book", "type": "snapshot", "data": [
                {"symbol": "BTC/USD",
                 "bids": [{"price": 67000, "qty": 0.5}, ...],
                 "asks": [{"price": 67010, "qty": 0.3}, ...]}
            ]}

        OFI delta = best_bid_qty - best_ask_qty
        Appended to the _ofi_deltas deque each time a book message arrives.

        Args:
            data: Parsed WebSocket message dict.
        """
        try:
            if data.get("channel") != "book":
                return
            book_data = data.get("data", [])
            if not book_data or not isinstance(book_data, list):
                return
            book = book_data[0]
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if not bids or not asks:
                return

            # Best bid = highest bid price (first in Kraken's sorted list)
            best_bid_qty = float(bids[0].get("qty", 0.0))
            # Best ask = lowest ask price (first in Kraken's sorted list)
            best_ask_qty = float(asks[0].get("qty", 0.0))

            delta = best_bid_qty - best_ask_qty
            self._ofi_deltas.append(delta)

        except (KeyError, IndexError, TypeError, ValueError):
            pass

    def _add_price_tick(self, price: float) -> None:
        """
        Add a new price tick to the rolling window.

        Stores the current timestamp with the price so the window can be
        trimmed to the last 5 minutes on each evaluation.

        Args:
            price: Current tick price.
        """
        now = datetime.now(UTC).timestamp()
        self._price_window.append((now, price))
        self._last_price = price
        self._last_update = datetime.now(UTC).isoformat()

    def _get_window_high(self) -> float:
        """
        Get the highest price in the last 5-minute rolling window.

        Filters the deque to entries within the last WINDOW_SECONDS seconds
        and returns the maximum price.

        Returns:
            Highest price in the window, or 0.0 if window is empty.
        """
        now = datetime.now(UTC).timestamp()
        cutoff = now - _WINDOW_SECONDS

        # Filter to only prices within the rolling window
        recent_prices = [price for ts, price in self._price_window if ts >= cutoff]

        if not recent_prices:
            return 0.0
        return max(recent_prices)

    def _check_shock_condition(self, current_price: float) -> bool:
        """
        Check if the current price represents a shock event.

        Args:
            current_price: The latest price tick.

        Returns:
            True if price drop from window high exceeds SHOCK_THRESHOLD_PCT.

        Example:
            >>> guard._check_shock_condition(64000.0)  # Window high was 67000
            True  # 4.5% drop → emergency
        """
        window_high = self._get_window_high()
        if window_high <= 0:
            return False

        drop_pct = (window_high - current_price) / window_high

        if drop_pct >= _SHOCK_THRESHOLD_PCT:
            logger.critical(
                f"SHOCK DETECTED: {drop_pct:.2%} drop from window high "
                f"${window_high:,.2f} to current ${current_price:,.2f} "
                f"(threshold: {_SHOCK_THRESHOLD_PCT:.0%})"
            )
            return True

        return False

    async def _trigger_emergency_exit(self) -> None:
        """
        Execute emergency exit: close all positions and activate circuit breaker.

        This method is idempotent — calling it multiple times (e.g. on continued
        ticks after the drop) does nothing after the first trigger.
        """
        if self._emergency_triggered:
            return

        self._emergency_triggered = True
        logger.critical("EMERGENCY EXIT TRIGGERED — closing all positions immediately")

        # Import here to avoid circular import at module level
        from backend.execution.trader import close_all_positions
        from backend.memory.store import set_state

        try:
            results = close_all_positions()
            successful = sum(1 for r in results if r.success)
            logger.critical(
                f"Emergency close: {successful}/{len(results)} positions closed"
            )
        except Exception as exc:
            logger.critical(f"Emergency close FAILED: {exc}")

        # Activate circuit breaker in SQLite
        try:
            set_state("circuit_breaker_active", "true")
            set_state("circuit_breaker_triggered_by", "shock_guard")
            logger.critical("Circuit breaker activated by shock guard")
        except Exception as exc:
            logger.critical(f"Failed to set circuit breaker state: {exc}")

    def _parse_price_from_ticker(self, data: dict[str, Any]) -> float | None:
        """
        Extract the last trade price from a Kraken WebSocket v2 ticker message.

        Kraken v2 ticker format:
            {"channel": "ticker", "type": "update", "data": [{"symbol": "BTC/USD", "last": 67234.10, ...}]}

        Args:
            data: Parsed WebSocket message dict.

        Returns:
            Price as float, or None if this message does not contain price data.

        Example:
            >>> price = guard._parse_price_from_ticker(ws_message)
        """
        try:
            # Kraken WebSocket v2 format
            if data.get("channel") == "ticker":
                ticker_data = data.get("data", [])
                if ticker_data and isinstance(ticker_data, list):
                    return float(ticker_data[0].get("last", 0))

            # Alternative: legacy format with result key
            if "result" in data:
                result = data["result"]
                if isinstance(result, dict):
                    for pair_data in result.values():
                        if isinstance(pair_data, dict) and "c" in pair_data:
                            return float(pair_data["c"][0])

        except (KeyError, IndexError, TypeError, ValueError):
            pass

        return None

    async def _subscribe_channels(self, ws: Any) -> None:
        """
        Subscribe to ticker (price) and book (OFI) channels on Kraken WebSocket v2.

        Sends two subscription messages:
          - ticker: for shock detection (price)
          - book:   for OFI accumulation (bid/ask quantities, depth=1)

        Args:
            ws: Active WebSocket connection object.

        Example:
            >>> await guard._subscribe_channels(ws)
        """
        pair_ws = settings.trading_pair.replace("USD", "/USD")  # e.g. BTCUSD → BTC/USD

        # Ticker subscription (shock detection)
        await ws.send(json.dumps({
            "method": "subscribe",
            "params": {"channel": "ticker", "symbol": [pair_ws]},
        }))

        # Book subscription (OFI accumulation) — depth=1 gives best bid/ask only
        await ws.send(json.dumps({
            "method": "subscribe",
            "params": {"channel": "book", "symbol": [pair_ws], "depth": 1},
        }))

        logger.info(f"Shock guard subscribed to ticker + book (OFI): {pair_ws}")

    async def run(self) -> None:
        """
        Main async event loop for the shock guard.

        Connects to Kraken WebSocket, subscribes to the ticker channel,
        and monitors prices continuously. Auto-reconnects on disconnect
        with exponential backoff. Runs until the asyncio task is cancelled.

        Example:
            >>> guard = ShockGuard()
            >>> await guard.run()  # Runs indefinitely (cancel to stop)
        """
        backoff = _INITIAL_BACKOFF

        while True:
            try:
                logger.info(f"Shock guard connecting to {_WS_URL}…")
                async with websockets.connect(
                    _WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._is_running = True
                    backoff = _INITIAL_BACKOFF  # Reset on successful connection
                    logger.info("Shock guard WebSocket connected")

                    await self._subscribe_channels(ws)

                    async for raw_msg in ws:
                        try:
                            data = json.loads(raw_msg)
                        except json.JSONDecodeError:
                            continue

                        channel = data.get("channel", "")

                        # OFI: accumulate bid/ask deltas from book updates
                        if channel == "book":
                            self._accumulate_ofi(data)
                            continue

                        # Shock detection: monitor price via ticker
                        if channel == "ticker":
                            price = self._parse_price_from_ticker(data)
                            if price is None or price <= 0:
                                continue
                            self._add_price_tick(price)
                            if self._check_shock_condition(price):
                                await self._trigger_emergency_exit()

            except asyncio.CancelledError:
                logger.info("Shock guard task cancelled — shutting down")
                self._is_running = False
                break

            except Exception as exc:
                self._is_running = False
                logger.warning(
                    f"Shock guard WebSocket error: {exc}. "
                    f"Reconnecting in {backoff:.1f}s…"
                )
                await asyncio.sleep(backoff)
                # Exponential backoff with cap
                backoff = min(backoff * _BACKOFF_MULTIPLIER, _MAX_BACKOFF)


# Module-level singleton used by main.py and routes.py
shock_guard = ShockGuard()
