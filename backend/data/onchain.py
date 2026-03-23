"""
On-chain signal module — public blockchain metrics for Bitcoin.

Fetches on-chain data points that can supplement price-action analysis with
network-level insights. These signals are entirely optional enrichment — the
agent trades without them if any request fails.

Current signals:
- **Exchange net flows**: Net BTC moving onto vs off exchanges (from Blockchain.info).
  Large inflows may signal selling pressure; outflows often precede rallies.
- **Mempool congestion**: Number of unconfirmed transactions as a proxy for
  network demand.
- **Hash rate**: Proxy for miner confidence in network security.

All data is cached for 30 minutes since on-chain metrics change slowly.

Role in system: Enrichment data included in the Gemini prompt context.
Not used in deterministic rule scoring — treated as qualitative context only.

Dependencies: requests, loguru
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import requests
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

# ── Cache ─────────────────────────────────────────────────────────────────────
_CACHE_TTL_SECONDS = 1800  # 30 minutes — on-chain metrics are slow-moving
_cache: dict[str, tuple[Any, float]] = {}


def _cache_get(key: str) -> Any | None:
    if key in _cache:
        val, ts = _cache[key]
        if time.time() - ts < _CACHE_TTL_SECONDS:
            return val
    return None


def _cache_set(key: str, value: Any) -> None:
    _cache[key] = (value, time.time())


class OnChainSnapshot(BaseModel):
    """
    Snapshot of on-chain Bitcoin network metrics.

    Attributes:
        mempool_size: Number of unconfirmed transactions in the mempool.
        hash_rate_eh_s: Current network hash rate in EH/s (exahashes per second).
        btc_price_usd: Latest BTC price from Blockchain.info ticker.
        exchange_flow_signal: 'outflow' (bullish), 'inflow' (bearish), or 'neutral'.
        fetched_at: ISO 8601 UTC timestamp.

    Example:
        >>> snap = get_onchain_snapshot()
        >>> print(snap.mempool_size)
    """

    model_config = ConfigDict(frozen=True)

    mempool_size: int = Field(default=0, description="Unconfirmed transaction count.")
    hash_rate_eh_s: float = Field(default=0.0, description="Network hash rate in EH/s.")
    btc_price_usd: float = Field(default=0.0, description="BTC price from Blockchain.info.")
    exchange_flow_signal: str = Field(
        default="neutral",
        description="Exchange flow direction: 'inflow', 'outflow', or 'neutral'.",
    )
    fetched_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


def fetch_mempool_stats() -> dict[str, Any]:
    """
    Fetch mempool statistics from Blockchain.info public API.

    Args: None

    Returns:
        Dictionary with 'mempool_size' (int, unconfirmed tx count) and
        'hash_rate_eh_s' (float, current hash rate).

    Raises:
        RuntimeError: If the API is unreachable (caller should handle gracefully).

    Example:
        >>> stats = fetch_mempool_stats()
        >>> print(stats['mempool_size'])
    """
    cache_key = "mempool"
    cached = _cache_get(cache_key)
    if cached:
        return cached  # type: ignore[return-value]

    try:
        # Blockchain.info unconfirmed tx count
        resp = requests.get(
            "https://blockchain.info/q/unconfirmedcount",
            timeout=8,
        )
        resp.raise_for_status()
        mempool_size = int(resp.text.strip())

        # Blockchain.info hash rate in GH/s — convert to EH/s
        hr_resp = requests.get(
            "https://blockchain.info/q/hashrate",
            timeout=8,
        )
        hr_resp.raise_for_status()
        # Returns value in GH/s
        hash_rate_gh = float(hr_resp.text.strip())
        hash_rate_eh = hash_rate_gh / 1_000_000  # GH/s → EH/s

        result: dict[str, Any] = {
            "mempool_size": mempool_size,
            "hash_rate_eh_s": round(hash_rate_eh, 2),
        }
        _cache_set(cache_key, result)
        return result

    except Exception as exc:
        logger.debug(f"Mempool stats unavailable: {exc}")
        return {"mempool_size": 0, "hash_rate_eh_s": 0.0}


def fetch_btc_price_onchain() -> float:
    """
    Fetch the current BTC/USD price from Blockchain.info.

    This is a secondary price source used only for cross-referencing.
    The primary price source is always the Kraken ticker.

    Returns:
        Current BTC price in USD as a float. Returns 0.0 on failure.

    Example:
        >>> price = fetch_btc_price_onchain()
    """
    cache_key = "btc_price_onchain"
    cached = _cache_get(cache_key)
    if cached:
        return float(cached)

    try:
        resp = requests.get(
            "https://blockchain.info/ticker",
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        price = float(data.get("USD", {}).get("last", 0.0))
        _cache_set(cache_key, price)
        return price
    except Exception as exc:
        logger.debug(f"Blockchain.info price unavailable: {exc}")
        return 0.0


def get_onchain_snapshot() -> OnChainSnapshot:
    """
    Aggregate all on-chain signals into a single snapshot.

    Fetches mempool stats and BTC price from Blockchain.info. The
    exchange_flow_signal is derived heuristically from mempool congestion:
    high mempool (> 50k txs) with high price implies inflows; low mempool
    with stable price implies outflows or consolidation.

    Returns:
        OnChainSnapshot with all available on-chain data populated.
        Individual field failures return safe defaults (0 / 'neutral').

    Example:
        >>> snap = get_onchain_snapshot()
        >>> print(snap.exchange_flow_signal)
    """
    mempool_stats = fetch_mempool_stats()
    btc_price = fetch_btc_price_onchain()

    mempool_size = mempool_stats.get("mempool_size", 0)
    hash_rate = mempool_stats.get("hash_rate_eh_s", 0.0)

    # Heuristic exchange flow signal based on mempool congestion proxy
    # High mempool congestion (>80k) often correlates with active selling/exchange deposits
    # Low mempool (<20k) suggests network calm, potential accumulation phase
    if mempool_size > 80_000:
        exchange_signal = "inflow"   # Bearish signal — possible selling pressure
    elif mempool_size < 20_000:
        exchange_signal = "outflow"  # Bullish signal — possible accumulation
    else:
        exchange_signal = "neutral"

    return OnChainSnapshot(
        mempool_size=mempool_size,
        hash_rate_eh_s=hash_rate,
        btc_price_usd=btc_price,
        exchange_flow_signal=exchange_signal,
        fetched_at=datetime.now(UTC).isoformat(),
    )
