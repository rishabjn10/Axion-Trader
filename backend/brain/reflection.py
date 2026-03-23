"""
Trade reflection module — formats past trade performance as LLM memory context.

The reflection system gives the Gemini LLM access to a summary of recent
trading history, enabling it to learn from past decisions in the prompt context.
This is a form of in-context learning: by including what worked and what didn't,
the LLM can adjust its strategy without requiring fine-tuning.

Example reflection output:
    Past performance (last 10 trades):
    - Trade 1: bought BTC at $67,200, sold at $68,100, +1.34%, AI said: 'Strong RSI recovery...'
    - Trade 2: bought BTC at $65,500, not closed yet (open), AI said: '...'
    ...
    Win rate (last 10): 60.0% (6/10 profitable)

If fewer than 3 closed trades exist, returns an empty string — not enough
history to provide meaningful context, and including sparse data might mislead
the LLM into over-fitting to a tiny sample.

Role in system: Called once per standard cycle, just before the Gemini API
call, to enrich the prompt with historical performance data.

Dependencies: memory.store (SQLite), pydantic, loguru
"""

from __future__ import annotations

from typing import Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field


class TradeMemory(BaseModel):
    """
    Compact representation of a closed or open trade for reflection context.

    Attributes:
        action: 'buy' or 'sell'.
        pair: Trading pair symbol.
        entry_price: Price at which the position was opened.
        exit_price: Price at which the position was closed (None if still open).
        pnl_pct: Profit/loss as a percentage of entry price.
        reasoning: The LLM's original reasoning that led to this trade.
        outcome: 'profit', 'loss', or 'open'.

    Example:
        >>> tm = TradeMemory(action='buy', pair='BTCUSD', entry_price=67000.0, ...)
    """

    model_config = ConfigDict(frozen=True)

    action: Literal["buy", "sell"]
    pair: str
    entry_price: float
    exit_price: float | None
    pnl_pct: float | None
    reasoning: str
    outcome: Literal["profit", "loss", "open"]


def get_reflection_context(limit: int = 10) -> str:
    """
    Build a formatted string summarising the last N closed trades.

    Queries the SQLite database for recent trades and formats them as a
    human-readable text block suitable for inclusion in the Gemini prompt.
    Returns an empty string if fewer than 3 closed trades exist (minimum
    required for meaningful statistical context).

    Args:
        limit: Maximum number of trades to include in the reflection. Defaults to 10.

    Returns:
        Formatted multi-line string describing recent trade history and win rate.
        Returns '' if fewer than 3 closed trades are available.

    Example:
        >>> context = get_reflection_context(limit=10)
        >>> if context:
        ...     print("Including reflection in Gemini prompt")
    """
    # Import here to avoid circular imports (store imports settings which may import brain)
    from backend.memory.store import get_recent_trades

    try:
        # Fetch all trades for accurate open position count (no limit here)
        all_trades = get_recent_trades(limit=200)
        # Limit only applies to the performance history shown to the LLM
        trades = all_trades[:limit]
    except Exception as exc:
        logger.warning(f"Failed to fetch trades for reflection: {exc}")
        return ""

    if not all_trades:
        return ""

    # Filter to closed trades for statistical analysis
    closed_trades = [t for t in trades if t.get("status") == "closed" and t.get("pnl_pct") is not None]
    # Open count from ALL trades, not just the limited subset
    open_trades = [t for t in all_trades if t.get("status") == "open"]

    lines: list[str] = []

    # Always include open positions summary — critical so LLM knows current exposure
    if open_trades:
        total_open = len(open_trades)
        open_actions = {}
        for t in open_trades:
            a = t.get("action", "unknown")
            open_actions[a] = open_actions.get(a, 0) + 1
        exposure_str = ", ".join(f"{cnt} {act}" for act, cnt in open_actions.items())
        lines.append(
            f"CURRENT OPEN POSITIONS: {total_open} active trades ({exposure_str}). "
            f"You are already in the market — do NOT keep opening the same direction unless strongly justified."
        )

    if len(closed_trades) < 3:
        logger.debug(f"Too few closed trades ({len(closed_trades)}) for full reflection context")
        if not lines:
            return ""
        return "\n".join(lines)

    lines.append(f"\nPast performance (last {len(trades)} trades):")

    for i, trade in enumerate(trades[:limit], 1):
        action = trade.get("action", "unknown")
        pair = trade.get("pair", "BTCUSD")
        entry = trade.get("entry_price", 0.0)
        exit_price = trade.get("exit_price")
        pnl_pct = trade.get("pnl_pct")
        status = trade.get("status", "unknown")

        # Get the AI reasoning stored with this trade decision
        reasoning = trade.get("llm_reasoning", "No AI reasoning recorded")
        if reasoning and len(reasoning) > 150:
            reasoning = reasoning[:150] + "…"

        if status == "closed" and exit_price and pnl_pct is not None:
            pnl_sign = "+" if pnl_pct >= 0 else ""
            outcome = "profitable" if pnl_pct >= 0 else "loss"
            lines.append(
                f"- Trade {i}: {action}d {pair} at ${entry:,.2f}, "
                f"closed at ${exit_price:,.2f}, {pnl_sign}{pnl_pct:.2f}% ({outcome}). "
                f"AI reasoning: '{reasoning}'"
            )
        else:
            lines.append(
                f"- Trade {i}: {action}d {pair} at ${entry:,.2f} (still open). "
                f"AI reasoning: '{reasoning}'"
            )

    # Win rate calculation — closed trades only
    profitable = sum(1 for t in closed_trades if (t.get("pnl_pct") or 0) >= 0)
    total_closed = len(closed_trades)
    win_rate = (profitable / total_closed * 100) if total_closed > 0 else 0.0

    lines.append(
        f"\nWin rate (last {total_closed} closed trades): "
        f"{win_rate:.1f}% ({profitable}/{total_closed} profitable)"
    )

    context = "\n".join(lines)
    logger.debug(f"Reflection context built: {len(trades)} trades, win rate={win_rate:.1f}%")
    return context
