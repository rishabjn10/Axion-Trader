"""
Kraken CLI subprocess wrapper for market data acquisition.

This module is the single integration point between axion-trader and the Kraken
exchange. All market data (OHLCV candles, live ticker, order book, account balance,
and open orders) is retrieved by shelling out to the ``kraken`` CLI binary using
subprocess. This approach avoids maintaining a REST/WebSocket client and instead
leverages the official Kraken CLI which handles authentication, rate-limiting,
and API versioning.

Role in system: Called by the main agent loops on every cycle to provide fresh
market data to the indicator engine and risk management layer.

Dependencies: pandas, loguru, subprocess (stdlib), json (stdlib)

Retry policy: Every function retries up to 3 times with exponential backoff
(1s, 2s, 4s) before raising. This handles transient Kraken API errors.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Any

import pandas as pd
from loguru import logger

from backend.config.settings import settings

# ── Guard: verify Kraken CLI is on PATH at module import time ─────────────────
_KRAKEN_BIN = shutil.which("kraken")
if _KRAKEN_BIN is None:
    logger.critical(
        "Kraken CLI not found on PATH. "
        "Install it from https://github.com/krakenfx/kraken-cli and re-run."
    )
    import sys
    sys.exit(1)


def _run_kraken(args: list[str], timeout: int = 30) -> dict[str, Any] | list[Any]:
    """
    Execute a Kraken CLI command and return parsed JSON output.

    This is the low-level helper used by all public functions in this module.
    It runs the command, captures stdout/stderr, and parses the JSON response.
    Non-zero exit codes are treated as errors and raise RuntimeError.

    Args:
        args: CLI arguments to pass after the ``kraken`` binary name.
              Example: ['ohlc', '--pair', 'BTCUSD', '--interval', '60', '-o', 'json']
        timeout: Maximum seconds to wait for the subprocess. Defaults to 30.

    Returns:
        Parsed JSON response — either a dict or list depending on the endpoint.

    Raises:
        RuntimeError: If the CLI returns a non-zero exit code or invalid JSON.
        subprocess.TimeoutExpired: If the command exceeds ``timeout`` seconds.

    Example:
        >>> data = _run_kraken(['ticker', '--pair', 'BTCUSD', '-o', 'json'])
    """
    cmd = [_KRAKEN_BIN, *args]
    logger.debug(f"Running Kraken CLI: {' '.join(cmd)}")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Kraken CLI exited with code {result.returncode}. "
            f"stderr: {result.stderr.strip()}"
        )

    if not result.stdout.strip():
        raise RuntimeError("Kraken CLI returned empty output.")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Failed to parse Kraken CLI output as JSON: {exc}. "
            f"Raw output: {result.stdout[:500]}"
        ) from exc


def _with_retry(
    fn: Any,
    *args: Any,
    max_attempts: int = 3,
    **kwargs: Any,
) -> Any:
    """
    Call ``fn`` with exponential backoff retry on failure.

    Args:
        fn: Callable to invoke.
        *args: Positional arguments forwarded to ``fn``.
        max_attempts: Maximum number of attempts before re-raising. Defaults to 3.
        **kwargs: Keyword arguments forwarded to ``fn``.

    Returns:
        Return value of ``fn`` on success.

    Raises:
        Exception: Re-raises the last exception after all attempts are exhausted.

    Example:
        >>> df = _with_retry(fetch_ohlcv, 'BTCUSD', 60, 200)
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            wait = 2 ** (attempt - 1)  # 1s, 2s, 4s
            logger.warning(
                f"Attempt {attempt}/{max_attempts} failed for {fn.__name__}: {exc}. "
                f"Retrying in {wait}s…"
            )
            time.sleep(wait)

    raise RuntimeError(
        f"{fn.__name__} failed after {max_attempts} attempts"
    ) from last_exc


def fetch_ohlcv(pair: str, interval: int, count: int = 200) -> pd.DataFrame:
    """
    Fetch OHLCV candlestick data from Kraken via the CLI.

    Calls ``kraken ohlc {pair} --interval {interval} -o json`` and parses the
    response into a clean pandas DataFrame with proper dtypes.
    The DataFrame index is a DatetimeIndex in UTC.

    Args:
        pair: Kraken trading pair symbol, e.g. 'BTCUSD' or 'XBTUSD'.
        interval: Candle interval in minutes. Must be one of: 1, 5, 15, 30, 60, 240, 1440.
        count: Unused — Kraken CLI returns up to 720 candles per call. Kept for
               call-site compatibility.

    Returns:
        DataFrame with columns [timestamp, open, high, low, close, volume] where:
          - timestamp is a UTC datetime
          - open, high, low, close, volume are float64

    Raises:
        RuntimeError: If the Kraken CLI fails after 3 retry attempts.
        ValueError: If the response cannot be parsed into the expected OHLCV format.

    Example:
        >>> df = fetch_ohlcv('BTCUSD', 60)
        >>> print(df.shape)
    """
    def _fetch() -> pd.DataFrame:
        raw = _run_kraken([
            "ohlc",
            pair,
            "--interval", str(interval),
            "-o", "json",
        ])

        # Kraken CLI returns either a list of candles or a dict with the pair as key
        if isinstance(raw, dict):
            # Find the candle array — Kraken sometimes wraps in a dict
            candles = None
            for key, val in raw.items():
                if isinstance(val, list) and len(val) > 0 and isinstance(val[0], list):
                    candles = val
                    break
            if candles is None:
                raise ValueError(f"Cannot find OHLCV candles in response: {list(raw.keys())}")
        elif isinstance(raw, list):
            candles = raw
        else:
            raise ValueError(f"Unexpected OHLCV response type: {type(raw)}")

        # Kraken OHLCV format: [time, open, high, low, close, vwap, volume, count]
        # We drop vwap and count from the raw candles
        rows = []
        for candle in candles:
            if len(candle) < 7:
                continue
            rows.append({
                "timestamp": pd.Timestamp(int(candle[0]), unit="s", tz="UTC"),
                "open": float(candle[1]),
                "high": float(candle[2]),
                "low": float(candle[3]),
                "close": float(candle[4]),
                "volume": float(candle[6]),
            })

        if not rows:
            raise ValueError("No valid OHLCV rows parsed from Kraken response.")

        df = pd.DataFrame(rows)
        df = df.sort_values("timestamp").reset_index(drop=True)
        logger.debug(f"Fetched {len(df)} OHLCV candles for {pair} @ {interval}m")
        return df

    return _with_retry(_fetch)


def fetch_ticker(pair: str) -> dict[str, Any]:
    """
    Fetch current ticker data for a trading pair.

    Calls ``kraken ticker --pair {pair} -o json`` and returns the full
    ticker payload including last price, 24h high/low, and volume.

    Args:
        pair: Kraken trading pair symbol, e.g. 'BTCUSD'.

    Returns:
        Dictionary containing:
          - last_price (float): Most recent trade price.
          - high_24h (float): 24-hour high.
          - low_24h (float): 24-hour low.
          - volume_24h (float): 24-hour traded volume.
          - bid (float): Best bid price.
          - ask (float): Best ask price.
          - timestamp (str): ISO 8601 UTC timestamp.

    Raises:
        RuntimeError: If the Kraken CLI fails after 3 retry attempts.

    Example:
        >>> ticker = fetch_ticker('BTCUSD')
        >>> print(ticker['last_price'])
    """
    def _fetch() -> dict[str, Any]:
        raw = _run_kraken(["ticker", pair, "-o", "json"])

        # Normalise — CLI may return the ticker wrapped in a pair key
        if isinstance(raw, dict):
            # Look for pair data nested under a key
            ticker_data = None
            for key, val in raw.items():
                if isinstance(val, dict) and "c" in val:
                    ticker_data = val
                    break
                elif isinstance(val, dict) and "last" in val:
                    ticker_data = val
                    break

            if ticker_data is None:
                # Maybe the dict IS the ticker directly
                ticker_data = raw

            # Kraken REST ticker format uses single-letter keys
            def _get(data: dict, *keys: str) -> float:
                for k in keys:
                    if k in data:
                        v = data[k]
                        return float(v[0]) if isinstance(v, list) else float(v)
                return 0.0

            return {
                "last_price": _get(ticker_data, "c", "last_price", "last"),
                "high_24h": _get(ticker_data, "h", "high_24h", "high"),
                "low_24h": _get(ticker_data, "l", "low_24h", "low"),
                "volume_24h": _get(ticker_data, "v", "volume_24h", "volume"),
                "bid": _get(ticker_data, "b", "bid"),
                "ask": _get(ticker_data, "a", "ask"),
                "pair": pair,
                "timestamp": pd.Timestamp.utcnow().isoformat(),
            }
        else:
            raise ValueError(f"Unexpected ticker response type: {type(raw)}")

    return _with_retry(_fetch)


def fetch_order_book(pair: str) -> dict[str, Any]:
    """
    Fetch the current order book (bid/ask walls) for a pair.

    Calls ``kraken orderbook --pair {pair} -o json`` and returns the top
    levels of the order book.

    Args:
        pair: Kraken trading pair symbol.

    Returns:
        Dictionary with 'bids' and 'asks' keys, each containing a list of
        [price, volume] pairs sorted best-first.

    Raises:
        RuntimeError: If the Kraken CLI fails after 3 retry attempts.

    Example:
        >>> book = fetch_order_book('BTCUSD')
        >>> best_bid = book['bids'][0][0]
    """
    def _fetch() -> dict[str, Any]:
        raw = _run_kraken(["orderbook", pair, "-o", "json"])

        if isinstance(raw, dict):
            # Find bids and asks
            bids: list[Any] = []
            asks: list[Any] = []

            # Direct keys
            if "bids" in raw:
                bids = raw["bids"]
                asks = raw.get("asks", [])
            else:
                # Nested under pair key
                for val in raw.values():
                    if isinstance(val, dict) and "bids" in val:
                        bids = val["bids"]
                        asks = val.get("asks", [])
                        break

            return {"bids": bids, "asks": asks, "pair": pair}
        else:
            raise ValueError(f"Unexpected order book response: {type(raw)}")

    return _with_retry(_fetch)


def fetch_balance() -> dict[str, float]:
    """
    Fetch account balances for all assets held on Kraken.

    Calls ``kraken balance -o json`` using the trading API key credentials.
    Returns a dictionary mapping asset symbols to their available balances.

    Returns:
        Dictionary mapping asset symbol to float balance.
        Example: {'USD': 10000.0, 'XBT': 0.15, 'ETH': 2.5}

    Raises:
        RuntimeError: If the Kraken CLI fails or trading credentials are not configured.

    Example:
        >>> balances = fetch_balance()
        >>> usd = balances.get('USD', 0.0)
    """
    def _fetch() -> dict[str, float]:
        # Paper mode uses the local paper account; live uses real Kraken balance
        cmd = ["paper", "balance", "-o", "json"] if settings.is_paper_mode else ["balance", "-o", "json"]
        raw = _run_kraken(cmd)

        if isinstance(raw, dict):
            balances: dict[str, float] = {}

            # Paper mode returns: {"balances": {"USD": {"available": N, ...}}, "mode": "paper"}
            # Live mode returns:  {"ZUSD": "9876.54", "XXBT": "0.001", ...}
            source = raw.get("balances", raw) if "balances" in raw else raw
            for asset, value in source.items():
                if asset == "mode":
                    continue
                try:
                    if isinstance(value, dict):
                        # Paper: use 'available' balance
                        balances[asset] = float(value.get("available", value.get("total", 0)))
                    else:
                        balances[asset] = float(value)
                except (TypeError, ValueError):
                    pass
            return balances
        else:
            raise ValueError(f"Unexpected balance response type: {type(raw)}")

    return _with_retry(_fetch)


def fetch_open_orders() -> list[dict[str, Any]]:
    """
    Fetch all currently open orders on the account.

    Calls ``kraken openorders -o json`` and returns a list of order objects.

    Returns:
        List of open order dictionaries. Each order contains:
          - order_id (str): Kraken order ID.
          - pair (str): Trading pair.
          - type (str): 'buy' or 'sell'.
          - volume (float): Order volume.
          - price (float): Limit price (0.0 for market orders).
          - status (str): Order status string.

    Raises:
        RuntimeError: If the Kraken CLI fails after 3 retry attempts.

    Example:
        >>> orders = fetch_open_orders()
        >>> print(f"{len(orders)} open orders")
    """
    def _fetch() -> list[dict[str, Any]]:
        # Paper mode has its own open orders command
        cmd = ["paper", "orders", "-o", "json"] if settings.is_paper_mode else ["open-orders", "-o", "json"]
        raw = _run_kraken(cmd)

        orders: list[dict[str, Any]] = []

        if isinstance(raw, dict):
            # Paper mode: {"count": N, "mode": "paper", "open_orders": [...]}
            if "open_orders" in raw:
                for order_data in raw["open_orders"]:
                    if not isinstance(order_data, dict):
                        continue
                    orders.append({
                        "order_id": order_data.get("order_id", order_data.get("txid", "")),
                        "pair": order_data.get("pair", ""),
                        "type": order_data.get("type", order_data.get("side", "")),
                        "ordertype": order_data.get("order_type", "market"),
                        "volume": float(order_data.get("volume", order_data.get("vol", 0))),
                        "volume_exec": float(order_data.get("volume_exec", order_data.get("vol_exec", 0))),
                        "price": float(order_data.get("price", 0)),
                        "status": order_data.get("status", "open"),
                        "open_time": order_data.get("open_time", order_data.get("opentm", 0)),
                    })
            else:
                # Live mode: Kraken wraps orders under 'open' key with order IDs as keys
                order_map: dict[str, Any] = raw.get("open", raw)
                for order_id, order_data in order_map.items():
                    if not isinstance(order_data, dict):
                        continue
                    desc = order_data.get("descr", {})
                    orders.append({
                        "order_id": order_id,
                        "pair": desc.get("pair", ""),
                        "type": desc.get("type", ""),
                        "ordertype": desc.get("ordertype", ""),
                        "volume": float(order_data.get("vol", 0)),
                        "volume_exec": float(order_data.get("vol_exec", 0)),
                        "price": float(order_data.get("price", 0)),
                        "status": order_data.get("status", ""),
                        "open_time": order_data.get("opentm", 0),
                    })
        elif isinstance(raw, list):
            orders = raw  # type: ignore[assignment]

        return orders

    return _with_retry(_fetch)
