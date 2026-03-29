"""
Volume Profile — identifies the Point of Control (POC) and Value Area
from OHLCV data.

Volume Profile answers: "At what prices did traders do the most business?"
Unlike Bollinger Bands or standard S/R (which are price-based), Volume Profile
is volume-backed — levels are significant because real money traded there.

Key outputs:
  POC  — Point of Control: price level with the highest traded volume.
         Price gravitates back to POC in ranging markets; POC acts as
         strong support/resistance when price is away from it.
  VAH  — Value Area High: upper boundary of the 70% volume zone.
  VAL  — Value Area Low:  lower boundary of the 70% volume zone.

Trading interpretation:
  - Price inside value area (VAL < price < VAH) = fair value, mean-reversion
  - Price above VAH = bullish breakout (or premium to value)
  - Price below VAL = bearish breakdown (or discount to value)
  - Price returning to POC from outside = POC magnet trade

Role in system: Called once per standard cycle on the 1h OHLCV window.
Result is passed to confluence scoring as signals 9 and 10 (value area
position + POC distance).

Dependencies: pandas, numpy
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field


class VolumeProfileResult(BaseModel):
    """
    Volume Profile computed over a price history window.

    Attributes:
        poc_price: Point of Control — price bucket with highest volume.
        value_area_high: Upper boundary of 70% value area.
        value_area_low:  Lower boundary of 70% value area.
        total_volume: Aggregate volume used in the computation.
        price_in_value_area: True if current price is inside VAL–VAH.
        price_above_value_area: True if current price is above VAH.
        price_below_value_area: True if current price is below VAL.
        poc_distance_pct: Current price distance from POC as percentage.
        price_vs_poc: 'above', 'below', or 'at' POC.

    Example:
        >>> vp = compute_volume_profile(df_1h)
        >>> print(vp.poc_price, vp.price_in_value_area)
    """

    model_config = ConfigDict(frozen=True)

    poc_price: float = Field(description="Point of Control price level.")
    value_area_high: float = Field(description="Value Area High (70% volume upper bound).")
    value_area_low: float = Field(description="Value Area Low (70% volume lower bound).")
    total_volume: float = Field(ge=0.0)
    price_in_value_area: bool
    price_above_value_area: bool
    price_below_value_area: bool
    poc_distance_pct: float = Field(description="Current price distance from POC (%).")
    price_vs_poc: str = Field(description="'above', 'below', or 'at'.")


def compute_volume_profile(
    df: pd.DataFrame,
    n_bins: int = 100,
    value_area_pct: float = 0.70,
) -> VolumeProfileResult:
    """
    Compute the Volume Profile over a OHLCV DataFrame window.

    Distributes each candle's volume across the price range [low, high]
    proportionally, then accumulates into n_bins price buckets. The bucket
    with the highest accumulated volume is the POC. The Value Area is found
    by expanding from the POC outward until 70% of total volume is enclosed.

    Args:
        df: OHLCV DataFrame with columns [open, high, low, close, volume].
            Needs at least 10 rows for a meaningful profile.
        n_bins: Number of price buckets to use. Higher = finer resolution
                but more memory. Default 100.
        value_area_pct: Fraction of total volume that defines the Value Area.
                        Default 0.70 (standard 70% value area).

    Returns:
        VolumeProfileResult with POC, VAH, VAL, and current price signals.

    Raises:
        ValueError: If DataFrame is empty or missing required columns.

    Example:
        >>> vp = compute_volume_profile(df, n_bins=50)
        >>> if vp.price_below_value_area:
        ...     print("Price at discount — potential mean-reversion long")
    """
    required = {"high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns: {missing}")

    if len(df) < 5:
        raise ValueError(f"Need at least 5 candles, got {len(df)}")

    df = df.copy()
    for col in ("high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["high", "low", "close", "volume"])

    price_min = float(df["low"].min())
    price_max = float(df["high"].max())
    current_price = float(df["close"].iloc[-1])

    if price_max <= price_min:
        price_max = price_min * 1.001  # avoid zero-range edge case

    # Build price bins
    bins = np.linspace(price_min, price_max, n_bins + 1)
    bucket_mid = (bins[:-1] + bins[1:]) / 2.0
    volume_by_bucket = np.zeros(n_bins)

    # Distribute each candle's volume proportionally across the candle's price range
    for _, row in df.iterrows():
        low  = float(row["low"])
        high = float(row["high"])
        vol  = float(row["volume"])
        if vol <= 0 or high <= low:
            continue

        # Find buckets that overlap with [low, high]
        bucket_lows  = bins[:-1]
        bucket_highs = bins[1:]
        overlap_low  = np.maximum(bucket_lows,  low)
        overlap_high = np.minimum(bucket_highs, high)
        overlap      = np.maximum(0.0, overlap_high - overlap_low)
        range_width  = high - low
        weights      = overlap / range_width
        volume_by_bucket += weights * vol

    total_volume = float(volume_by_bucket.sum())
    if total_volume <= 0:
        logger.warning("Volume profile: total volume is zero, returning midpoint defaults")
        mid = (price_min + price_max) / 2.0
        return VolumeProfileResult(
            poc_price=mid,
            value_area_high=price_max,
            value_area_low=price_min,
            total_volume=0.0,
            price_in_value_area=True,
            price_above_value_area=False,
            price_below_value_area=False,
            poc_distance_pct=0.0,
            price_vs_poc="at",
        )

    # POC = bucket with highest volume
    poc_idx   = int(np.argmax(volume_by_bucket))
    poc_price = float(bucket_mid[poc_idx])

    # Value Area: expand from POC outward until value_area_pct of volume is captured
    target_vol = total_volume * value_area_pct
    accumulated = float(volume_by_bucket[poc_idx])
    lo_idx = poc_idx
    hi_idx = poc_idx

    while accumulated < target_vol:
        can_go_up   = hi_idx + 1 < n_bins
        can_go_down = lo_idx - 1 >= 0

        if not can_go_up and not can_go_down:
            break

        up_vol   = float(volume_by_bucket[hi_idx + 1]) if can_go_up   else -1.0
        down_vol = float(volume_by_bucket[lo_idx - 1]) if can_go_down else -1.0

        if up_vol >= down_vol:
            hi_idx    += 1
            accumulated += up_vol
        else:
            lo_idx    -= 1
            accumulated += down_vol

    vah = float(bins[hi_idx + 1])   # upper edge of the highest-included bucket
    val = float(bins[lo_idx])        # lower edge of the lowest-included bucket

    # Classify current price position
    price_in_va    = val <= current_price <= vah
    price_above_va = current_price > vah
    price_below_va = current_price < val

    poc_dist_pct = round((current_price - poc_price) / poc_price * 100, 3) if poc_price > 0 else 0.0
    if abs(poc_dist_pct) < 0.1:
        price_vs_poc = "at"
    elif poc_dist_pct > 0:
        price_vs_poc = "above"
    else:
        price_vs_poc = "below"

    logger.debug(
        f"Volume Profile: POC=${poc_price:,.2f} VAL=${val:,.2f} VAH=${vah:,.2f} "
        f"price=${current_price:,.2f} ({price_vs_poc} POC, {poc_dist_pct:+.2f}%)"
    )

    return VolumeProfileResult(
        poc_price=round(poc_price, 2),
        value_area_high=round(vah, 2),
        value_area_low=round(val, 2),
        total_volume=round(total_volume, 4),
        price_in_value_area=price_in_va,
        price_above_value_area=price_above_va,
        price_below_value_area=price_below_va,
        poc_distance_pct=poc_dist_pct,
        price_vs_poc=price_vs_poc,
    )
