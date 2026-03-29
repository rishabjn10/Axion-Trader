"""
Trade state machine for backtesting.

Tracks open/closed positions, applies stop/TP logic on each candle, calculates
P&L with realistic fees, and produces summary statistics per phase.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import pandas as pd

TAKER_FEE        = 0.0026  # Kraken standard taker fee: 0.26% per side (default)
MAX_HOLD_CANDLES = 576     # Max 5m candles a position may stay open (576 × 5m = 48h).
                           # 48h gives trends enough time to reach TP before forced exit.


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class ClosedTrade:
    phase:              int
    entry_time:         datetime
    exit_time:          datetime
    action:             Literal["buy", "sell"]
    entry_price:        float
    exit_price:         float
    stop_price:         float
    take_profit_price:  float
    position_usd:       float
    pnl_usd:            float
    pnl_pct:            float
    exit_reason:        Literal["stop_loss", "take_profit", "end_of_data"]
    balance_after:      float
    confluence_score:   int | None = None
    rule_triggered:     str | None = None
    llm_confidence:     float | None = None


@dataclass
class _OpenTrade:
    """Internal — not exposed outside this module."""
    phase:              int
    entry_time:         datetime
    action:             Literal["buy", "sell"]
    entry_price:        float
    stop_price:         float
    take_profit_price:  float
    position_usd:       float
    confluence_score:   int | None = None
    rule_triggered:     str | None = None
    llm_confidence:     float | None = None


# ── Portfolio ─────────────────────────────────────────────────────────────────

class Portfolio:
    """
    Independent portfolio for one backtest phase.

    Each phase (1, 2, 3) gets its own Portfolio starting at the same balance,
    so their performance can be compared on equal footing.
    """

    def __init__(self, starting_balance: float, phase: int, fee: float = TAKER_FEE) -> None:
        self.phase             = phase
        self.fee               = fee   # per-side fee fraction (e.g. 0.0026 = 0.26%)
        self.balance           = starting_balance
        self.starting_balance  = starting_balance
        self._open: list[_OpenTrade]   = []
        self.closed: list[ClosedTrade] = []
        self._peak             = starting_balance
        self.max_drawdown_pct  = 0.0
        # (timestamp, balance) pairs appended on every trade close — used for equity curve sheet
        self.equity_curve: list[tuple[datetime, float]] = []

    # ── Public interface ───────────────────────────────────────────────────────

    def open_count(self) -> int:
        return len(self._open)

    def exposure_pct(self) -> float:
        """Fraction of current balance currently deployed in open positions."""
        if self.balance <= 0:
            return 1.0
        return sum(t.position_usd for t in self._open) / self.balance

    def check_exits(self, candle: pd.Series) -> None:
        """
        Scan every open position against the candle's high/low.
        Closes any position whose stop or TP was hit during this candle.
        """
        remaining: list[_OpenTrade] = []
        ts = pd.Timestamp(candle["timestamp"]).to_pydatetime()

        for trade in self._open:
            reason = _exit_reason(trade, candle)
            # Max-hold exit: force-close if position has been open too long.
            # Prevents capital being locked for days on 5m timeframe.
            if reason is None and MAX_HOLD_CANDLES > 0:
                hold = int((ts - trade.entry_time).total_seconds() / 60 / 5)
                if hold >= MAX_HOLD_CANDLES:
                    reason = "end_of_data"  # reuse closest exit type
            if reason:
                exit_px = trade.stop_price if reason == "stop_loss" else (
                    trade.take_profit_price if reason == "take_profit" else float(candle["close"])
                )
                self._close(trade, exit_px, ts, reason)
            else:
                remaining.append(trade)

        self._open = remaining

    def enter(
        self,
        candle: pd.Series,
        action: Literal["buy", "sell"],
        approval,                       # TradeApproval from risk/guard.py
        confluence_score: int | None = None,
        rule_triggered:   str | None = None,
        llm_confidence:   float | None = None,
    ) -> None:
        """Open a new position. Entry fee is immediately deducted from balance."""
        entry_price  = float(candle["close"])
        position_usd = approval.position_size_usd
        fee          = position_usd * self.fee
        self.balance -= fee

        self._open.append(_OpenTrade(
            phase             = self.phase,
            entry_time        = pd.Timestamp(candle["timestamp"]).to_pydatetime(),
            action            = action,
            entry_price       = entry_price,
            stop_price        = approval.stop_price,
            take_profit_price = approval.take_profit_price,
            position_usd      = position_usd,
            confluence_score  = confluence_score,
            rule_triggered    = rule_triggered,
            llm_confidence    = llm_confidence,
        ))

    def close_all(self, last_price: float, last_ts: datetime) -> None:
        """Close all remaining open positions at end of backtest data."""
        for trade in list(self._open):
            self._close(trade, last_price, last_ts, "end_of_data")
        self._open = []

    def compute_stats(self) -> dict:
        """Return a dict of summary statistics for this phase."""
        if not self.closed:
            return self._empty_stats()

        wins   = [t for t in self.closed if t.pnl_usd > 0]
        losses = [t for t in self.closed if t.pnl_usd <= 0]

        n            = len(self.closed)
        win_rate     = len(wins) / n
        avg_win_pct  = sum(t.pnl_pct for t in wins)   / len(wins)   if wins   else 0.0
        avg_loss_pct = sum(t.pnl_pct for t in losses) / len(losses) if losses else 0.0

        gross_wins  = sum(t.pnl_usd for t in wins)
        gross_loss  = abs(sum(t.pnl_usd for t in losses))
        profit_factor = (
            round(gross_wins / gross_loss, 2) if gross_loss > 0
            else (999.0 if gross_wins > 0 else 0.0)
        )

        expectancy  = (win_rate * avg_win_pct) + ((1 - win_rate) * avg_loss_pct)

        total_return_pct = (self.balance - self.starting_balance) / self.starting_balance * 100

        # Break-even win rate: WR needed so EV = 0
        denom = avg_win_pct + abs(avg_loss_pct)
        break_even_wr = abs(avg_loss_pct) / denom if denom > 0 else 0.0

        durations_h = [
            (t.exit_time - t.entry_time).total_seconds() / 3600
            for t in self.closed
        ]

        return {
            "total_trades":        n,
            "wins":                len(wins),
            "losses":              len(losses),
            "win_rate_pct":        round(win_rate * 100, 2),
            "avg_win_pct":         round(avg_win_pct, 3),
            "avg_loss_pct":        round(avg_loss_pct, 3),
            "profit_factor":       profit_factor,
            "expectancy_pct":      round(expectancy, 3),
            "total_return_pct":    round(total_return_pct, 2),
            "final_balance_usd":   round(self.balance, 2),
            "max_drawdown_pct":    round(self.max_drawdown_pct, 2),
            "sharpe_annualised":   round(self._sharpe(), 3),
            "break_even_wr_pct":   round(break_even_wr * 100, 2),
            "stop_hits":           sum(1 for t in self.closed if t.exit_reason == "stop_loss"),
            "tp_hits":             sum(1 for t in self.closed if t.exit_reason == "take_profit"),
            "eod_hits":            sum(1 for t in self.closed if t.exit_reason == "end_of_data"),
            "avg_duration_hours":  round(sum(durations_h) / len(durations_h), 1) if durations_h else 0.0,
        }

    def monthly_returns(self) -> dict[tuple[int, int], float]:
        """Return {(year, month): return_pct} from closed trade PnL."""
        result: dict[tuple[int, int], float] = defaultdict(float)
        for t in self.closed:
            key = (t.exit_time.year, t.exit_time.month)
            result[key] += t.pnl_usd / self.starting_balance * 100
        return dict(result)

    # ── Private helpers ────────────────────────────────────────────────────────

    def _close(self, trade: _OpenTrade, exit_price: float, ts: datetime, reason: str) -> None:
        fee = trade.position_usd * self.fee
        if trade.action == "buy":
            gross = (exit_price - trade.entry_price) / trade.entry_price * trade.position_usd
        else:
            gross = (trade.entry_price - exit_price) / trade.entry_price * trade.position_usd
        net     = gross - fee
        pnl_pct = net / trade.position_usd * 100

        self.balance += net
        self._update_drawdown()
        self.equity_curve.append((ts, round(self.balance, 2)))

        self.closed.append(ClosedTrade(
            phase             = trade.phase,
            entry_time        = trade.entry_time,
            exit_time         = ts,
            action            = trade.action,
            entry_price       = trade.entry_price,
            exit_price        = exit_price,
            stop_price        = trade.stop_price,
            take_profit_price = trade.take_profit_price,
            position_usd      = trade.position_usd,
            pnl_usd           = round(net, 4),
            pnl_pct           = round(pnl_pct, 4),
            exit_reason       = reason,
            balance_after     = round(self.balance, 2),
            confluence_score  = trade.confluence_score,
            rule_triggered    = trade.rule_triggered,
            llm_confidence    = trade.llm_confidence,
        ))

    def _update_drawdown(self) -> None:
        if self.balance > self._peak:
            self._peak = self.balance
        dd = (self._peak - self.balance) / self._peak * 100
        if dd > self.max_drawdown_pct:
            self.max_drawdown_pct = dd

    def _sharpe(self) -> float:
        """Annualised Sharpe ratio computed from daily PnL of closed trades."""
        if len(self.closed) < 2:
            return 0.0
        daily: dict[str, float] = defaultdict(float)
        for t in self.closed:
            daily[t.exit_time.strftime("%Y-%m-%d")] += t.pnl_usd / self.starting_balance
        returns = list(daily.values())
        if len(returns) < 2:
            return 0.0
        mean = sum(returns) / len(returns)
        var  = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        std  = math.sqrt(var)
        return (mean / std * math.sqrt(252)) if std > 0 else 0.0

    def _empty_stats(self) -> dict:
        return {
            "total_trades": 0, "wins": 0, "losses": 0,
            "win_rate_pct": 0.0, "avg_win_pct": 0.0, "avg_loss_pct": 0.0,
            "profit_factor": 0.0, "expectancy_pct": 0.0,
            "total_return_pct": 0.0, "final_balance_usd": round(self.balance, 2),
            "max_drawdown_pct": 0.0, "sharpe_annualised": 0.0,
            "break_even_wr_pct": 0.0, "stop_hits": 0, "tp_hits": 0,
            "eod_hits": 0, "avg_duration_hours": 0.0,
        }


# ── Module-level helper ────────────────────────────────────────────────────────

def _exit_reason(
    trade: _OpenTrade, candle: pd.Series
) -> Literal["stop_loss", "take_profit"] | None:
    lo = float(candle["low"])
    hi = float(candle["high"])
    if trade.action == "buy":
        if lo <= trade.stop_price:
            return "stop_loss"
        if hi >= trade.take_profit_price:
            return "take_profit"
    else:                           # sell / short
        if hi >= trade.stop_price:
            return "stop_loss"
        if lo <= trade.take_profit_price:
            return "take_profit"
    return None
