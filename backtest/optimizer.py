"""
AI-powered strategy optimizer — analyses backtest results and suggests config improvements.

Sends the backtest trade log, summary stats, and current settings to the Gemini LLM.
Receives 5 concrete parameter combinations ranked by expected improvement, then:
  - Prints them to the console as a Rich table
  - Saves them to backtest/results/optimize_PAIR_YYYYMMDD_HHMMSS.json

Usage (called automatically after every backtest unless --no-optimize is passed):
    from backtest.optimizer import run_optimizer
    run_optimizer(portfolios, pair, output_dir)

Or standalone:
    python -m backtest.optimizer --result backtest/results/backtest_BTCUSD_20260330.xlsx
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from rich.console import Console
from rich.table import Table

from backend.config.settings import settings

if TYPE_CHECKING:
    from backtest.simulator import Portfolio

console = Console()


def _current_config() -> dict:
    """Return strategy params from settings as a flat dict (no secrets)."""
    return {
        "trading_pair":           settings.trading_pair,
        "confluence_min_score":   settings.confluence_min_score,
        "confidence_threshold":   settings.confidence_threshold,
        "max_position_pct":       settings.max_position_pct,
        "stop_loss_pct":          settings.stop_loss_pct,
        "max_open_positions":     settings.max_open_positions,
        "max_hold_hours":         settings.max_hold_hours,
        # RSI thresholds
        "rsi_oversold":           settings.rsi_oversold,
        "rsi_overbought":         settings.rsi_overbought,
        "rsi_soft_oversold":      settings.rsi_soft_oversold,
        "rsi_soft_overbought":    settings.rsi_soft_overbought,
        "rsi_bull_min":           settings.rsi_bull_min,
        "rsi_bull_max":           settings.rsi_bull_max,
        "rsi_bear_min":           settings.rsi_bear_min,
        "rsi_bear_max":           settings.rsi_bear_max,
        # Risk / reward
        "atr_stop_multiplier":    settings.atr_stop_multiplier,
        "tp_ratio":               settings.tp_ratio,
        # Regime detection
        "adx_strong_threshold":   settings.adx_strong_threshold,
        "adx_weak_threshold":     settings.adx_weak_threshold,
        "atr_volatile_zscore":    settings.atr_volatile_zscore,
        # Rule confidences
        "rule_conf_extreme":      settings.rule_conf_extreme,
        "rule_conf_cross":        settings.rule_conf_cross,
        "rule_conf_state":        settings.rule_conf_state,
        "rule_conf_ranging":      settings.rule_conf_ranging,
    }


def _summarise_portfolios(portfolios: list["Portfolio"]) -> list[dict]:
    """Extract key stats from each portfolio (phases or walk-forward windows)."""
    summaries = []
    # Detect walk-forward mode: all portfolios have the same phase number
    phases = [p.phase for p in portfolios]
    is_walk_forward = len(set(phases)) == 1 and len(portfolios) > 1

    for i, p in enumerate(portfolios):
        s = p.compute_stats()
        label = f"window_{i+1}" if is_walk_forward else f"phase_{p.phase}"
        summaries.append({
            "label":             label,
            "total_trades":      s["total_trades"],
            "win_rate_pct":      s["win_rate_pct"],
            "profit_factor":     s["profit_factor"],
            "total_return_pct":  s["total_return_pct"],
            "max_drawdown_pct":  s["max_drawdown_pct"],
            "sharpe":            s["sharpe_annualised"],
            "stop_hits":         s["stop_hits"],
            "tp_hits":           s["tp_hits"],
            "eod_hits":          s["eod_hits"],
            "avg_duration_h":    s["avg_duration_hours"],
        })
    return summaries


def _build_prompt(phase_summaries: list[dict], config: dict) -> str:
    """Build the Gemini prompt from backtest results and current config."""
    phase_text = json.dumps(phase_summaries, indent=2)
    config_text = json.dumps(config, indent=2)

    is_walk_forward = any("window_" in s.get("label", "") for s in phase_summaries)
    results_header = (
        "Walk-forward windows (each is an independent out-of-sample test period):"
        if is_walk_forward
        else "Backtest results (3 phases):\nPhase 1 = rule engine only\nPhase 2 = confluence gate + rules (production)\nPhase 3 = full pipeline + LLM"
    )

    return f"""You are an expert quantitative trading strategist analysing backtesting results for a crypto trading bot.

## Current configuration
```json
{config_text}
```

## {results_header}

```json
{phase_text}
```

## Your task
Analyse these results and provide **exactly 5 alternative parameter configurations** that are likely to improve performance.

Focus on:
- If win_rate < 45%: tighten entry rules (raise rsi_oversold, lower rsi_overbought, raise confluence_min_score)
- If eod_hits > 20% of trades: reduce max_hold_hours or tighten stop/TP
- If stop_hits > 60% of trades: increase atr_stop_multiplier or widen rsi thresholds
- If total_trades < 20: loosen confluence_min_score or widen RSI ranges
- If profit_factor < 1.0: raise confidence_threshold, raise tp_ratio
- If max_drawdown > 15%: lower max_position_pct, raise stop quality

Constraints:
- Only modify the parameters shown in the config above
- Keep rsi_oversold < rsi_soft_oversold < 50
- Keep rsi_overbought > rsi_soft_overbought > 50
- Keep rsi_bull_min < rsi_bull_max
- Keep rsi_bear_min < rsi_bear_max
- Keep adx_weak_threshold < adx_strong_threshold
- Keep tp_ratio >= 1.5

Respond with **only valid JSON** — no markdown, no explanation outside the JSON:
{{
  "analysis": "2-3 sentence summary of what the results show and the key problem to fix",
  "suggestions": [
    {{
      "rank": 1,
      "label": "short descriptive name",
      "rationale": "one sentence explaining why this config should improve results",
      "changes": {{
        "param_name": new_value,
        ...
      }}
    }},
    ...
  ]
}}"""


def _call_gemini(prompt: str) -> dict | None:
    """Call Gemini API and return parsed JSON response."""
    try:
        import google.generativeai as genai
        genai.configure(api_key=settings.gemini_api_key)
        model = genai.GenerativeModel("gemini-3.1-flash-lite-preview")
        response = model.generate_content(
            prompt,
            generation_config={"temperature": 0.3, "response_mime_type": "application/json"},
        )
        text = response.text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)
    except Exception as exc:
        logger.warning(f"Optimizer: Gemini call failed — {exc}")
        return None


def _print_results(result: dict) -> None:
    """Print analysis + suggestion table to the console."""
    console.print()
    console.rule("[bold magenta]AI Strategy Optimizer[/bold magenta]")
    console.print(f"\n[bold]Analysis:[/bold] {result.get('analysis', '')}\n")

    table = Table(title="Suggested Configurations", show_lines=True)
    table.add_column("#", style="bold cyan", width=3)
    table.add_column("Label", style="bold", min_width=20)
    table.add_column("Rationale", min_width=40)
    table.add_column("Changes", min_width=40)

    for s in result.get("suggestions", []):
        changes_str = "\n".join(f"{k} = {v}" for k, v in s.get("changes", {}).items())
        table.add_row(
            str(s.get("rank", "")),
            s.get("label", ""),
            s.get("rationale", ""),
            changes_str,
        )

    console.print(table)
    console.print()


def _save_results(result: dict, pair: str, output_dir: Path) -> Path:
    """Save optimizer results to a JSON file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = output_dir / f"optimize_{pair}_{ts}.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    return out_path


def run_optimizer(
    portfolios: list["Portfolio"],
    pair: str,
    output_dir: Path,
) -> None:
    """
    Analyse backtest results with Gemini and save/print 5 config suggestions.

    Args:
        portfolios: List of Portfolio objects from the backtest (phases 1, 2, 3).
        pair: Trading pair string (e.g. 'SOLUSD').
        output_dir: Directory where the JSON results file is written.
    """
    console.print("\n[dim]Running AI strategy optimizer…[/dim]")

    config = _current_config()
    phase_summaries = _summarise_portfolios(portfolios)
    prompt = _build_prompt(phase_summaries, config)

    result = _call_gemini(prompt)
    if result is None:
        console.print("[yellow]Optimizer: Gemini unavailable — skipping suggestions.[/yellow]")
        return

    _print_results(result)

    # Attach metadata before saving
    result["pair"]         = pair
    result["generated_at"] = datetime.now().isoformat()
    result["config_used"]  = config

    out_path = _save_results(result, pair, output_dir)
    console.print(f"[green bold]✓ Optimizer results saved:[/green bold] {out_path}\n")
