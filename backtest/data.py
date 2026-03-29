"""
Historical OHLCV data fetching via yfinance with local CSV caching.

yfinance provides:
  - 5m  interval: up to 60 days   (~17,280 candles for BTC-USD)
  - 1h  interval: up to 730 days  (~17,489 candles for BTC-USD)
  - 1d  interval: full history    (10+ years)

Kraken's public OHLC REST API hard-caps at 720 candles per request with no
working pagination — it always returns the most recent 720 candles regardless
of the `since` parameter. yfinance is used instead for historical backtesting.

Usage:
    from backtest.data import load_history
    df_5m = load_history("BTCUSD", 5)         # 60 days of 5m candles
    df_1h = load_history("BTCUSD", 60)        # 2 years of 1h candles
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import yfinance as yf

CACHE_DIR = Path(__file__).parent / "cache"

# Yahoo Finance ticker symbols
_YF_TICKER: dict[str, str] = {
    "BTCUSD": "BTC-USD",
    "XBTUSD": "BTC-USD",
    "ETHUSD": "ETH-USD",
    "SOLUSD": "SOL-USD",
}

# Map interval in minutes → yfinance interval string
_YF_INTERVAL: dict[int, str] = {
    1:    "1m",
    2:    "2m",
    5:    "5m",
    15:   "15m",
    30:   "30m",
    60:   "1h",
    240:  "4h",
    1440: "1d",
}

# Map interval in minutes → maximum period yfinance supports
_YF_MAX_PERIOD: dict[int, str] = {
    1:    "7d",
    2:    "60d",
    5:    "60d",
    15:   "60d",
    30:   "60d",
    60:   "730d",
    240:  "730d",
    1440: "max",
}


def _yf_ticker(pair: str) -> str:
    return _YF_TICKER.get(pair.upper(), pair.upper().replace("USD", "-USD"))


def _fetch_yf(ticker: str, interval: str, period: str) -> pd.DataFrame:
    """Download OHLCV from Yahoo Finance and normalise to standard column names."""
    raw = yf.download(
        ticker,
        period=period,
        interval=interval,
        progress=False,
        auto_adjust=True,
    )
    if raw.empty:
        raise RuntimeError(f"yfinance returned no data for {ticker} {interval}")

    raw = raw.reset_index()

    # Flatten MultiIndex columns produced by some yfinance versions
    raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]

    # Timestamp column is "Datetime" for intraday, "Date" for daily
    ts_col = "Datetime" if "Datetime" in raw.columns else "Date"
    raw = raw.rename(columns={
        ts_col:   "timestamp",
        "Open":   "open",
        "High":   "high",
        "Low":    "low",
        "Close":  "close",
        "Volume": "volume",
    })

    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    return raw[["timestamp", "open", "high", "low", "close", "volume"]].dropna()


def load_history(pair: str, interval: int, years: int = 2) -> pd.DataFrame:
    """
    Return a complete OHLCV history DataFrame for pair/interval.

    Downloads from Yahoo Finance on first call and caches to CSV.
    On subsequent calls, loads the cache and merges a fresh download to fill
    any gap since the last cached candle.

    Args:
        pair:     Trading pair, e.g. 'BTCUSD'.
        interval: Candle interval in minutes (5, 15, 60, 240).
        years:    Ignored for intervals ≤ 30m (yfinance limits those to 60 days).
                  For 60m+, controls how far back to fetch (up to 2 years).

    Returns:
        DataFrame sorted ascending with columns:
        timestamp (UTC-aware), open, high, low, close, volume.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{pair.upper()}_{interval}m.csv"

    ticker      = _yf_ticker(pair)
    yf_interval = _YF_INTERVAL[interval]
    period      = _YF_MAX_PERIOD.get(interval, "730d")

    if cache_file.exists():
        cached = pd.read_csv(cache_file)
        cached["timestamp"] = pd.to_datetime(cached["timestamp"], utc=True)
        print(
            f"  [{pair} {interval}m] cache: {len(cached)} rows. "
            f"Merging fresh download…"
        )
        fresh = _fetch_yf(ticker, yf_interval, period)
        df = (
            pd.concat([cached, fresh])
            .drop_duplicates(subset="timestamp")
            .sort_values("timestamp")
            .reset_index(drop=True)
        )
    else:
        print(f"  [{pair} {interval}m] No cache — downloading from Yahoo Finance…")
        df = _fetch_yf(ticker, yf_interval, period)

    df.to_csv(cache_file, index=False)
    print(
        f"  [{pair} {interval}m] {len(df)} candles  "
        f"({df['timestamp'].min().date()} → {df['timestamp'].max().date()})"
    )
    return df
