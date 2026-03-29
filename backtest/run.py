"""
Backtest runner — replays historical OHLCV through three independent strategy phases.

Phase 1  Rule engine only: no confluence gate, no LLM.
Phase 2  Confluence gate + rule engine.
Phase 3  Full pipeline: confluence gate + rule engine + Gemini LLM (responses cached).

Each phase runs on an identical starting balance with its own independent Portfolio
so their results can be compared on equal footing.

Walk-forward analysis (--walk-forward):
  Splits the full history into train/test windows and runs Phase 2 on each test
  window only. Produces a table of window-by-window results to detect overfitting.
  Default: 12-month train, 3-month test, 3-month step (configurable via --wf-train
  and --wf-test).

Fee sensitivity sweep (--fee-sweep):
  Re-runs Phase 2 at multiple fee levels (0.05%, 0.10%, 0.16%, 0.26%, 0.40%) and
  shows return and win rate at each level so you can identify the break-even fee.
  Use --fee FLOAT to set a custom single fee for the standard run.

Usage
-----
    python -m backtest.run
    python -m backtest.run --pair BTCUSD --years 2 --balance 10000 --no-llm
    python -m backtest.run --walk-forward --wf-train 12 --wf-test 3
    python -m backtest.run --fee-sweep
    python -m backtest.run --fee 0.0016
    pdm run backtest
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from loguru import logger
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TimeRemainingColumn
from rich.table import Table

from backtest.data import load_history
from backtest.report import write_excel
from backtest.simulator import Portfolio

from backend.config.settings import settings
from backend.data.sentiment import SentimentSnapshot

console = Console()

# ── Tuning constants ───────────────────────────────────────────────────────────
# Primary timeframe is 5m; regime context uses 1h candles.
WARMUP      = 50     # 5m candles skipped before first signal (indicator warmup)
                     # RSI=14, MACD=35, BB=20, EMA=21 → 50 five-min candles is safe
IND_WINDOW  = 200    # rolling window of 5m candles passed to compute_indicators
                     # 200 × 5m = ~17h of data for RSI/MACD/BB/EMA
REGIME_1H_W = 100    # 1h candle window for regime EMA slope detection (~4 days)
REGIME_4H_W = 50     # 1h candle window for regime ADX detection (~2 days)
MAX_OPEN    = settings.max_open_positions  # pulled from .env MAX_OPEN_POSITIONS

# Walk-forward defaults (in days, not months — 5m data covers max 60 days)
WF_TRAIN_DAYS = 30   # days of data used for "training" context (warmup source)
WF_TEST_DAYS  = 7    # days of data scored in each out-of-sample test window
WF_STEP_DAYS  = 7    # days to advance the window each iteration

# Keep month aliases so existing callers don't break
WF_TRAIN_MONTHS = WF_TRAIN_DAYS
WF_TEST_MONTHS  = WF_TEST_DAYS
WF_STEP_MONTHS  = WF_STEP_DAYS

# Mock sentiment — constant for the entire backtest so results are deterministic
_MOCK_SENTIMENT = SentimentSnapshot(
    fear_greed_value=50,
    fear_greed_classification="Neutral",
    overall_news_sentiment="neutral",
    fetched_at=datetime.now(timezone.utc).isoformat(),
)


# ── LLM cache helpers ──────────────────────────────────────────────────────────

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _load_llm_cache(path: Path) -> dict[str, dict]:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def _save_llm_cache(path: Path, cache: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2))


# ── Rich summary table ─────────────────────────────────────────────────────────

def _print_summary(portfolios: list[Portfolio]) -> None:
    table = Table(
        title="Backtest Results",
        show_header=True,
        header_style="bold blue",
        show_lines=True,
    )
    table.add_column("Metric", style="dim", min_width=26)
    for p in portfolios:
        table.add_column(f"Phase {p.phase}", justify="right", min_width=12)

    stats = [p.compute_stats() for p in portfolios]

    rows: list[tuple[str, str]] = [
        ("Total Trades",           "total_trades"),
        ("Wins",                   "wins"),
        ("Losses",                 "losses"),
        ("Win Rate %",             "win_rate_pct"),
        ("Avg Win %",              "avg_win_pct"),
        ("Avg Loss %",             "avg_loss_pct"),
        ("Profit Factor",          "profit_factor"),
        ("Expectancy %",           "expectancy_pct"),
        ("Total Return %",         "total_return_pct"),
        ("Final Balance USD",      "final_balance_usd"),
        ("Max Drawdown %",         "max_drawdown_pct"),
        ("Sharpe (annualised)",    "sharpe_annualised"),
        ("Break-Even Win Rate %",  "break_even_wr_pct"),
        ("Stop / TP / EoD exits",  "_combined"),
    ]

    for label, key in rows:
        if key == "_combined":
            vals = [
                f"{s['stop_hits']}/{s['tp_hits']}/{s['eod_hits']}"
                for s in stats
            ]
        else:
            vals = []
            for s in stats:
                v = s.get(key, "-")
                vals.append(f"{v:.2f}" if isinstance(v, float) else str(v))
        table.add_row(label, *vals)

    console.print("\n", table)


# ── Candle loop helper ─────────────────────────────────────────────────────────

def _run_candle_loop(
    df_5m: pd.DataFrame,
    df_1h: pd.DataFrame,
    portfolios: list,                  # list[Portfolio] — modified in place
    llm_ctx: dict,                     # {enabled, get_decision, MarketSnapshot, aggregate, GeminiDecision, cache, calls, hits}
    progress,
    task,
    fee: float = 0.0026,
) -> None:
    """
    Replay 5m candles through Phase 1/2/3 logic, filling the given portfolios.

    Primary timeframe: 5m (signal generation via compute_indicators).
    Context timeframe: 1h (regime detection — only recalculated on new 1h close).

    Extracted as a helper so walk-forward can call it on subsets of the data.
    The portfolios list must contain exactly 3 Portfolio objects (P1, P2, P3).
    """
    from backend.indicators.engine import compute_indicators
    from backend.indicators.regime import detect_regime
    from backend.brain.rules import evaluate as rule_evaluate
    from backend.indicators.confluence import score as conf_score_fn
    from backend.risk.guard import approve_trade

    p1, p2, p3 = portfolios
    n = len(df_5m)
    last_1h_ts: pd.Timestamp | None = None
    regime_ctx = None

    for i in range(WARMUP, n):
        candle = df_5m.iloc[i]
        ts     = candle["timestamp"]

        p1.check_exits(candle)
        p2.check_exits(candle)
        p3.check_exits(candle)

        # 5m indicator window — used for RSI, MACD, EMA, BB, ATR signals
        start = max(0, i - IND_WINDOW + 1)
        w_5m  = df_5m.iloc[start : i + 1].copy()

        # Regime detection on 1h context — only recalculate when a new 1h candle closes
        w_1h_slow = df_1h[df_1h["timestamp"] <= ts].tail(REGIME_4H_W)
        if not w_1h_slow.empty:
            latest_1h_ts = w_1h_slow["timestamp"].iloc[-1]
            if latest_1h_ts != last_1h_ts and len(w_1h_slow) >= 20:
                w_1h_fast = df_1h[df_1h["timestamp"] <= ts].tail(REGIME_1H_W)
                if len(w_1h_fast) >= 20:
                    try:
                        regime_ctx = detect_regime(w_1h_fast, w_1h_slow)
                    except Exception:
                        pass
                last_1h_ts = latest_1h_ts

        if regime_ctx is None:
            if progress:
                progress.advance(task)
            continue

        try:
            ind = compute_indicators(w_5m)
        except Exception:
            if progress:
                progress.advance(task)
            continue

        conf = conf_score_fn(ind, _MOCK_SENTIMENT)
        rule = rule_evaluate(ind, regime_ctx.regime)

        # Phase 1: Rule engine only
        if p1.open_count() < MAX_OPEN and rule.action != "hold":
            appr = approve_trade(rule.action, rule.confidence, ind.current_price, p1.balance, ind.atr)
            if appr.approved:
                p1.enter(candle, rule.action, appr, confluence_score=conf.score, rule_triggered=rule.triggered_rule)

        # Phase 2: Confluence gate + rules
        if p2.open_count() < MAX_OPEN and conf.passes_threshold and rule.action != "hold":
            appr = approve_trade(rule.action, rule.confidence, ind.current_price, p2.balance, ind.atr)
            if appr.approved:
                p2.enter(candle, rule.action, appr, confluence_score=conf.score, rule_triggered=rule.triggered_rule)

        # Phase 3: Confluence gate + rules + LLM
        if p3.open_count() < MAX_OPEN and conf.passes_threshold:
            action3 = "hold"
            conf3   = 0.0
            llm_enabled   = llm_ctx.get("enabled", False)
            _get_decision = llm_ctx.get("get_decision")
            _MSnap        = llm_ctx.get("MarketSnapshot")
            _aggregate    = llm_ctx.get("aggregate")
            _GDec         = llm_ctx.get("GeminiDecision")
            llm_cache     = llm_ctx.get("cache", {})

            if llm_enabled and _get_decision and _MSnap and _aggregate:
                msnap = _MSnap(
                    pair=settings.trading_pair,
                    current_price=ind.current_price, rsi=ind.rsi,
                    macd_cross=ind.macd_cross_direction, macd_histogram=ind.macd_histogram,
                    bb_pct_b=ind.bb_pct_b, bb_upper=ind.bb_upper, bb_lower=ind.bb_lower,
                    vwap=ind.vwap, ema_fast=ind.ema_fast, ema_slow=ind.ema_slow,
                    ema_cross=ind.ema_cross, atr=ind.atr, adx=ind.adx,
                    confluence_score=conf.score, confluence_direction=conf.dominant_direction,
                    signal_breakdown=conf.signal_breakdown, regime=regime_ctx.regime.value,
                    fear_greed_value=_MOCK_SENTIMENT.fear_greed_value,
                    fear_greed_label=_MOCK_SENTIMENT.fear_greed_classification,
                    news_sentiment=_MOCK_SENTIMENT.overall_news_sentiment,
                )
                cache_key = _sha256(msnap.model_dump_json())
                if cache_key in llm_cache:
                    gemini_dec = _GDec(**llm_cache[cache_key])
                    llm_ctx["hits"] = llm_ctx.get("hits", 0) + 1
                else:
                    gemini_dec = _get_decision(msnap, reflection="")
                    llm_ctx["calls"] = llm_ctx.get("calls", 0) + 1
                    llm_cache[cache_key] = gemini_dec.model_dump()
                    if llm_ctx["calls"] % 10 == 0:
                        _save_llm_cache(Path("backtest/cache/llm_cache.json"), llm_cache)
                final_dec = _aggregate(gemini_dec, rule)
                action3   = final_dec.action
                conf3     = final_dec.final_confidence
            else:
                action3 = rule.action
                conf3   = rule.confidence

            if action3 != "hold":
                appr = approve_trade(action3, conf3, ind.current_price, p3.balance, ind.atr)
                if appr.approved:
                    p3.enter(candle, action3, appr, confluence_score=conf.score,
                             rule_triggered=rule.triggered_rule,
                             llm_confidence=conf3 if llm_enabled else None)

        if progress:
            progress.advance(task)


# ── Walk-forward analysis ──────────────────────────────────────────────────────

def _print_walk_forward(windows: list[dict]) -> None:
    """Print walk-forward window results as a Rich table."""
    table = Table(
        title="Walk-Forward Analysis (Phase 2 — Confluence + Rules)",
        show_header=True, header_style="bold magenta", show_lines=True,
    )
    table.add_column("Window",          style="dim",    min_width=22)
    table.add_column("Trades",          justify="right", min_width=7)
    table.add_column("Win Rate %",      justify="right", min_width=10)
    table.add_column("Total Return %",  justify="right", min_width=14)
    table.add_column("Sharpe",          justify="right", min_width=8)
    table.add_column("Max DD %",        justify="right", min_width=9)

    for w in windows:
        s = w["stats"]
        label = f"{w['test_start']} → {w['test_end']}"
        ret_style = "green" if s["total_return_pct"] >= 0 else "red"
        table.add_row(
            label,
            str(s["total_trades"]),
            f"{s['win_rate_pct']:.1f}",
            f"[{ret_style}]{s['total_return_pct']:+.2f}[/{ret_style}]",
            f"{s['sharpe_annualised']:.2f}",
            f"{s['max_drawdown_pct']:.2f}",
        )

    console.print("\n", table)

    if not windows:
        console.print("  [yellow]No walk-forward windows fit in the available data.[/yellow]\n")
        return

    # Aggregate across windows
    total_trades = sum(w["stats"]["total_trades"] for w in windows)
    avg_return   = sum(w["stats"]["total_return_pct"] for w in windows) / len(windows)
    avg_sharpe   = sum(w["stats"]["sharpe_annualised"] for w in windows) / len(windows)
    console.print(
        f"  [dim]Totals:[/dim] {total_trades} trades across {len(windows)} windows  |  "
        f"Avg return/window: [bold]{avg_return:+.2f}%[/bold]  |  "
        f"Avg Sharpe: [bold]{avg_sharpe:.2f}[/bold]\n"
    )


def run_walk_forward(
    df_5m: pd.DataFrame,
    df_1h: pd.DataFrame,
    starting_balance: float = 10_000.0,
    train_months: int = WF_TRAIN_DAYS,
    test_months:  int = WF_TEST_DAYS,
    step_months:  int = WF_STEP_DAYS,
    fee: float = 0.0026,
) -> list[dict]:
    """
    Run walk-forward analysis by sliding a train/test window across the 5m data.

    For each window:
      - The *train* period precedes the test window and provides warm-up candles.
      - The *test* period is where Phase 2 trades are evaluated (out-of-sample).

    Args:
        df_5m:            Full 5m OHLCV DataFrame (primary signals).
        df_1h:            Full 1h OHLCV DataFrame (regime context).
        starting_balance: Portfolio starting balance per window.
        train_months:     Days before the test window (for indicator warmup).
        test_months:      Out-of-sample window size in days.
        step_months:      How many days to advance the window each iteration.
        fee:              Per-side trade fee fraction.

    Returns:
        List of dicts with keys: test_start, test_end, stats (from compute_stats).
    """
    from backtest.simulator import Portfolio

    train_days = train_months
    test_days  = test_months
    step_days  = step_months

    console.print(
        f"\n[bold magenta]Walk-Forward Analysis[/bold magenta]  "
        f"(train={train_days}d / test={test_days}d / step={step_days}d)\n"
    )

    df_5m = df_5m.sort_values("timestamp").reset_index(drop=True)
    df_1h = df_1h.sort_values("timestamp").reset_index(drop=True)

    # Convert day-based windows to 5m candle counts (288 five-min candles per day)
    CANDLES_PER_DAY = 288
    train_len = train_days * CANDLES_PER_DAY
    test_len  = test_days  * CANDLES_PER_DAY
    step_len  = step_days  * CANDLES_PER_DAY

    n       = len(df_5m)
    results = []
    window_start = train_len  # first test window starts after train_len candles

    while window_start + test_len <= n:
        window_end = window_start + test_len

        # The slice fed to the loop includes the train portion for warmup
        slice_start   = max(0, window_start - train_len)
        df_slice_5m   = df_5m.iloc[slice_start : window_end].reset_index(drop=True)
        slice_ts_min  = df_5m.iloc[slice_start]["timestamp"]
        slice_ts_max  = df_5m.iloc[window_end - 1]["timestamp"]
        df_slice_1h   = df_1h[
            (df_1h["timestamp"] >= slice_ts_min) &
            (df_1h["timestamp"] <= slice_ts_max)
        ].reset_index(drop=True)

        if len(df_slice_1h) < 20:
            window_start += step_len
            continue

        p1_wf = Portfolio(starting_balance, phase=1, fee=fee)
        p2_wf = Portfolio(starting_balance, phase=2, fee=fee)
        p3_wf = Portfolio(starting_balance, phase=3, fee=fee)

        real_warmup = min(window_start - slice_start, len(df_slice_5m) - 1)

        import backtest.run as _self
        _orig = _self.WARMUP
        _self.WARMUP = real_warmup

        try:
            _run_candle_loop(
                df_slice_5m, df_slice_1h,
                [p1_wf, p2_wf, p3_wf],
                {"enabled": False},
                progress=None, task=None, fee=fee,
            )
        finally:
            _self.WARMUP = _orig

        last_price = float(df_slice_5m.iloc[-1]["close"])
        last_ts    = df_slice_5m.iloc[-1]["timestamp"].to_pydatetime()
        p2_wf.close_all(last_price, last_ts)

        test_start = df_5m.iloc[window_start]["timestamp"].strftime("%Y-%m-%d")
        test_end   = df_5m.iloc[min(window_end - 1, n - 1)]["timestamp"].strftime("%Y-%m-%d")

        results.append({
            "test_start": test_start,
            "test_end":   test_end,
            "stats":      p2_wf.compute_stats(),
        })

        window_start += step_len

    _print_walk_forward(results)
    return results


# ── Fee sensitivity sweep ──────────────────────────────────────────────────────

def run_fee_sweep(
    df_5m: pd.DataFrame,
    df_1h: pd.DataFrame,
    starting_balance: float = 10_000.0,
) -> None:
    """
    Re-run Phase 2 at multiple fee levels to identify the break-even fee.

    Fee levels tested: 0.05%, 0.10%, 0.16% (maker), 0.26% (taker), 0.40%, 0.60%
    """
    from backtest.simulator import Portfolio

    fee_levels = [0.0005, 0.0010, 0.0016, 0.0026, 0.0040, 0.0060]

    table = Table(
        title="Fee Sensitivity Analysis (Phase 2 — Confluence + Rules)",
        show_header=True, header_style="bold cyan", show_lines=True,
    )
    table.add_column("Fee / Side",      style="dim",     min_width=12)
    table.add_column("Round-Trip",      justify="right", min_width=12)
    table.add_column("Trades",          justify="right", min_width=8)
    table.add_column("Win Rate %",      justify="right", min_width=10)
    table.add_column("Total Return %",  justify="right", min_width=14)
    table.add_column("Profit Factor",   justify="right", min_width=13)

    console.print("\n[bold cyan]Fee Sensitivity Sweep[/bold cyan]\n")

    for fee in fee_levels:
        p1_fs = Portfolio(starting_balance, phase=1, fee=fee)
        p2_fs = Portfolio(starting_balance, phase=2, fee=fee)
        p3_fs = Portfolio(starting_balance, phase=3, fee=fee)

        _run_candle_loop(
            df_5m, df_1h, [p1_fs, p2_fs, p3_fs],
            {"enabled": False},
            progress=None, task=None, fee=fee,
        )

        last_price = float(df_5m.iloc[-1]["close"])
        last_ts    = df_5m.iloc[-1]["timestamp"].to_pydatetime()
        p2_fs.close_all(last_price, last_ts)

        s = p2_fs.compute_stats()
        ret_style = "green" if s["total_return_pct"] >= 0 else "red"

        table.add_row(
            f"{fee*100:.2f}%",
            f"{fee*2*100:.2f}%",
            str(s["total_trades"]),
            f"{s['win_rate_pct']:.1f}",
            f"[{ret_style}]{s['total_return_pct']:+.2f}[/{ret_style}]",
            f"{s['profit_factor']:.2f}",
        )

    console.print(table, "\n")


# ── Main backtest function ─────────────────────────────────────────────────────

def run_backtest(
    pair: str = "BTCUSD",
    years: int = 2,
    starting_balance: float = 10_000.0,
    llm_enabled: bool = True,
    output_dir: Path | None = None,
    fee: float = 0.0026,
) -> None:
    if output_dir is None:
        output_dir = Path("backtest/results")

    # Silence loguru during the tight candle loop — keep console clean
    logger.disable("backend")

    console.rule("[bold blue]Axion Trader — Backtest[/bold blue]")
    console.print(
        f"  Pair: [bold]{pair}[/bold]  |  "
        f"Years: {years}  |  "
        f"Balance: ${starting_balance:,.2f}\n"
    )

    # ── Load OHLCV data ────────────────────────────────────────────────────────
    console.print("[dim]Loading OHLCV data from Yahoo Finance / cache…[/dim]")
    df_5m = load_history(pair, 5)         # 60 days of 5m candles (primary signals)
    df_1h = load_history(pair, 60, years) # 2 years of 1h candles (regime context)

    # Ensure UTC-aware timestamps
    for df in (df_5m, df_1h):
        if df["timestamp"].dt.tz is None:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    n = len(df_5m)
    console.print(
        f"\n  [green]✓[/green] {n} 5m candles  "
        f"({df_5m['timestamp'].min().date()} → {df_5m['timestamp'].max().date()})"
    )
    console.print(
        f"  [green]✓[/green] {len(df_1h)} 1h candles  "
        f"({df_1h['timestamp'].min().date()} → {df_1h['timestamp'].max().date()})"
    )

    if n < WARMUP + 20:
        console.print(f"[red]Not enough candles ({n}) — need ≥ {WARMUP + 20}. Aborting.[/red]")
        sys.exit(1)

    # ── Portfolios ─────────────────────────────────────────────────────────────
    p1 = Portfolio(starting_balance, phase=1, fee=fee)
    p2 = Portfolio(starting_balance, phase=2, fee=fee)
    p3 = Portfolio(starting_balance, phase=3, fee=fee)

    # ── LLM setup ─────────────────────────────────────────────────────────────
    _get_decision   = None
    _MarketSnapshot = None
    _aggregate      = None

    if llm_enabled:
        try:
            from backend.brain.gemini import GeminiDecision, MarketSnapshot, get_decision
            from backend.brain.aggregator import aggregate
            _get_decision   = get_decision
            _MarketSnapshot = MarketSnapshot
            _aggregate      = aggregate
            _GeminiDecision = GeminiDecision
            console.print("  [green]✓[/green] Gemini LLM available — Phase 3 enabled")
        except Exception as exc:
            console.print(
                f"  [yellow]⚠[/yellow] Gemini unavailable ({exc})\n"
                "  Phase 3 will mirror Phase 2 (rules + confluence, no LLM)."
            )
            llm_enabled = False

    if not llm_enabled:
        try:
            from backend.brain.aggregator import aggregate
            _aggregate = aggregate
        except Exception:
            pass

    llm_cache_path = Path("backtest/cache/llm_cache.json")
    llm_cache: dict[str, dict] = _load_llm_cache(llm_cache_path)

    llm_ctx: dict = {
        "enabled":       llm_enabled,
        "get_decision":  _get_decision,
        "MarketSnapshot": _MarketSnapshot,
        "aggregate":     _aggregate,
        "GeminiDecision": locals().get("_GeminiDecision"),
        "cache":         llm_cache,
        "calls":         0,
        "hits":          0,
    }

    # ── Main candle loop ───────────────────────────────────────────────────────
    with Progress(
        SpinnerColumn(),
        "[progress.description]{task.description}",
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Replaying candles…", total=n - WARMUP)
        _run_candle_loop(df_5m, df_1h, [p1, p2, p3], llm_ctx, progress, task, fee=fee)

    llm_calls  = llm_ctx["calls"]
    cache_hits = llm_ctx["hits"]

    # ── Persist LLM cache ──────────────────────────────────────────────────────
    if llm_calls > 0:
        _save_llm_cache(llm_cache_path, llm_cache)
        console.print(
            f"\n  LLM API calls: [cyan]{llm_calls}[/cyan]  |  "
            f"Cache hits: [green]{cache_hits}[/green]"
        )

    # ── Close all remaining open positions at last candle price ───────────────
    last_price = float(df_5m.iloc[-1]["close"])
    last_ts    = df_5m.iloc[-1]["timestamp"].to_pydatetime()
    p1.close_all(last_price, last_ts)
    p2.close_all(last_price, last_ts)
    p3.close_all(last_price, last_ts)

    # ── Print results ──────────────────────────────────────────────────────────
    _print_summary([p1, p2, p3])

    # ── Write Excel report ─────────────────────────────────────────────────────
    config_snapshot: dict = {
        "pair":                   pair,
        "primary_timeframe":      "5m",
        "context_timeframe":      "1h",
        "days_5m_history":        (df_5m["timestamp"].max() - df_5m["timestamp"].min()).days,
        "years_1h_context":       years,
        "starting_balance_usd":   starting_balance,
        "fee_per_side_pct":       round(fee * 100, 4),
        "round_trip_fee_pct":     round(fee * 2 * 100, 4),
        "confidence_threshold":   settings.confidence_threshold,
        "stop_loss_pct":          settings.stop_loss_pct,
        "max_position_pct":       settings.max_position_pct,
        "confluence_min_score":   settings.confluence_min_score,
        "warmup_candles":         WARMUP,
        "indicator_window_5m":    IND_WINDOW,
        "max_open_per_portfolio": MAX_OPEN,
        "llm_enabled":            llm_enabled,
        "llm_cache_hits":         cache_hits,
        "llm_api_calls":          llm_calls,
        "generated_at_utc":       datetime.now(timezone.utc).isoformat(),
    }

    ts_str   = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = output_dir / f"backtest_{pair}_{ts_str}.xlsx"
    excel    = write_excel([p1, p2, p3], config_snapshot, out_path)
    console.print(f"\n[green bold]✓ Report saved:[/green bold] {excel}\n")


# ── CLI entry point ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Axion Trader — 3-phase historical backtest",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--pair",         default="BTCUSD",    help="Trading pair")
    parser.add_argument("--years",        default=2, type=int, help="Years of history to replay")
    parser.add_argument("--balance",      default=10_000.0, type=float, help="Starting balance in USD")
    parser.add_argument("--no-llm",       action="store_true", help="Disable Phase 3 Gemini LLM calls")
    parser.add_argument("--output",       default="backtest/results", help="Output directory for .xlsx report")
    parser.add_argument("--fee",          default=0.0026, type=float, help="Per-side trade fee (e.g. 0.0026 = 0.26%%)")
    parser.add_argument("--fee-sweep",    action="store_true", help="Run fee sensitivity analysis across multiple fee levels")
    parser.add_argument("--walk-forward", action="store_true", help="Run walk-forward analysis instead of full-period backtest")
    parser.add_argument("--wf-train",     default=WF_TRAIN_DAYS, type=int, help="Walk-forward: train window in days (default 30)")
    parser.add_argument("--wf-test",      default=WF_TEST_DAYS,  type=int, help="Walk-forward: test window in days (default 7)")
    parser.add_argument("--wf-step",      default=WF_STEP_DAYS,  type=int, help="Walk-forward: step size in days (default 7)")
    args = parser.parse_args()

    if args.walk_forward or args.fee_sweep:
        # Load data once for analysis modes
        console.rule("[bold blue]Axion Trader — Backtest Analysis[/bold blue]")
        console.print(f"  Pair: [bold]{args.pair}[/bold]  |  Years: {args.years}  |  Balance: ${args.balance:,.2f}\n")
        console.print("[dim]Loading OHLCV data…[/dim]")
        from backtest.data import load_history
        df_5m = load_history(args.pair, 5)
        df_1h = load_history(args.pair, 60, args.years)
        for df in (df_5m, df_1h):
            if df["timestamp"].dt.tz is None:
                df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        logger.disable("backend")

        if args.walk_forward:
            run_walk_forward(
                df_5m, df_1h,
                starting_balance=args.balance,
                train_months=args.wf_train,
                test_months=args.wf_test,
                step_months=args.wf_step,
                fee=args.fee,
            )

        if args.fee_sweep:
            run_fee_sweep(df_5m, df_1h, starting_balance=args.balance)

    else:
        run_backtest(
            pair=args.pair,
            years=args.years,
            starting_balance=args.balance,
            llm_enabled=not args.no_llm,
            output_dir=Path(args.output),
            fee=args.fee,
        )


if __name__ == "__main__":
    main()
