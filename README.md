# axion-trader

Autonomous AI trading agent for Kraken exchange — Gemini Flash LLM + deterministic rule engine + multi-timeframe confluence + three-tier risk management.

> **⚠️ Trading Risk Disclaimer**
> This software is for educational and research purposes. Cryptocurrency trading carries significant financial risk — you can lose your entire investment. Past simulated (paper) performance is not indicative of future real results. This is not financial advice. Never trade with money you cannot afford to lose. The authors accept no liability for financial losses incurred through use of this software.

```
                       ┌─────────────────────────────────────────────────┐
                       │                  axion-trader                   │
                       └─────────────────────────────────────────────────┘

 ┌──────────────┐   ┌──────────────────┐   ┌────────────────────┐   ┌─────────────┐
 │  Kraken CLI  │──▶│   Indicators     │──▶│     AI Brain       │──▶│    Risk     │
 │  OHLCV  5m   │   │  10 signals      │   │  Gemini Flash LLM  │   │  3-tier     │
 │  OHLCV  1h   │   │  confluence      │   │  + Rule Engine     │   │  guard      │
 │  WebSocket   │   │  regime (5-state)│   │  + Narrative Ctx   │   │             │
 └──────────────┘   └──────────────────┘   └────────────────────┘   └─────────────┘
        │                                                                    │
 ┌──────────────┐   ┌──────────────┐   ┌──────────────────┐                │
 │  Sentiment   │   │ Microstructure│   │  Volume Profile  │                │
 │  F&G + news  │   │  OFI + spread │   │  POC / VAH / VAL │                │
 └──────────────┘   └──────────────┘   └──────────────────┘                │
                                                                            │
 ┌──────────────────────────────────────────────────────────────────────────▼──────┐
 │                          SQLite  (trading.db)                                   │
 │   decisions · trades · portfolio_snapshots · agent_state · execution_quality   │
 └──────────────────────────────────────────────────────────────────────┬──────────┘
                                                                        │
 ┌──────────────────────────────────────────────────────────────────────▼──────────┐
 │                        FastAPI REST API  (:8000)                                │
 │   /api/state · /api/trades · /api/metrics · /api/decisions · /api/price        │
 └──────────────────────────────────────────────────────────────────────┬──────────┘
                                                                        │
 ┌──────────────────────────────────────────────────────────────────────▼──────────┐
 │                       React Dashboard  (:3000)                                  │
 │    Agent Status · PnL Cards · Trade Log · Price Chart · Confluence Panel        │
 └─────────────────────────────────────────────────────────────────────────────────┘
```

## Features

- **Multi-timeframe analysis** — 5m (signals) + 1h (regime context) OHLCV from Kraken/yfinance
- **10 confluence signals** — RSI, MACD cross, Bollinger Bands %B, VWAP, EMA cross, ATR, ADX, market regime, microstructure, volume profile; each votes bull/bear; weighted score gates the AI cycle
- **5-state market regime** — `TRENDING_UP_STRONG`, `TRENDING_UP_WEAK`, `RANGING`, `TRENDING_DOWN`, `VOLATILE`; VOLATILE triggers stand-aside early exit
- **8-rule deterministic engine** — Rules 1–4 (event-based: RSI extreme + BB + MACD cross, EMA cross with regime confirmation) and Rules 5–8 (state-based: EMA momentum, RSI bounce/fade in ranging markets)
- **Hybrid AI brain** — Gemini Flash LLM (strategic context) + deterministic rule engine (tactical triggers); consensus required to execute
- **Narrative context** — LLM-generated market narrative adjusts confidence modifier and raises confluence threshold when tail risk is detected
- **Order Flow Imbalance (OFI)** — rolling bid/ask quantity delta from Kraken Level 2 WebSocket, normalised to [-1, +1], used as market microstructure signal
- **Volume profile** — intrabar Point of Control (POC), Value Area High/Low computed on 1h data; used as support/resistance context in confluence
- **LLM memory / reflection** — recent trade history injected into every Gemini prompt so the AI reasons about what it has already done
- **Three-tier risk management** — per-trade sizing (Kelly or fixed %), portfolio exposure cap, circuit breaker
- **ATR-based stop-loss with floor** — `max(ATR × 1.5, STOP_LOSS_PCT × entry)` so the configured percentage acts as a minimum guarantee
- **2:1 R:R enforcement** — take-profit is always 2× the actual stop distance
- **Execution quality tracking** — signal price vs actual fill price + slippage % recorded per order in `execution_quality` table
- **Flash crash protection** — WebSocket shock guard: 3% drop in 5 min triggers emergency close of all positions
- **Full DB audit trail** — every cycle type writes a record: fast loop evaluations, circuit breaker skips, regime changes, portfolio snapshots on every standard cycle
- **Confluence signal breakdown** — per-signal bull/bear/neutral stored in DB, returned by API, displayed visually on dashboard, injected into Gemini prompt
- **Paper / Live modes** — full simulation via `kraken paper` commands; zero code changes required to switch
- **React dashboard** — real-time status, PnL metrics, trade log with stop/target/closed-at, indicator panel with signal breakdown
- **Docker support** — `docker compose up -d` for 24/7 background operation
- **Historical backtester** — 3-phase backtest (rules only / confluence + rules / + LLM) with walk-forward analysis and fee sensitivity sweep

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) + Docker Compose (recommended)
- Or natively: Python 3.13+, [PDM](https://pdm-project.org/), Node.js 20+
- [Kraken CLI](https://github.com/krakenfx/kraken-cli) — `v0.2.2+`
- Kraken account with API keys
- Google Gemini API key

## Quick Start — Docker (recommended)

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/axion-trader
cd axion-trader

# 2. Configure environment
cp .env.example .env
# Edit .env — fill in GEMINI_API_KEY and Kraken API keys

# 3. Build and start (agent + API + dashboard, restarts automatically)
docker compose up -d --build

# 4. Open dashboard
open http://localhost:3000

# View live logs
docker compose logs -f backend
```

> **macOS note:** Docker containers restart automatically on crashes, but when your Mac sleeps the Docker VM suspends too. To keep the agent running 24/7, either set macOS Energy Saver to never sleep, or deploy to a VPS (`docker compose up -d` works identically there).

## Quick Start — Local Dev

```bash
# 1. Install PDM and Python deps
pip install pdm
pdm install

# 2. Configure environment
cp .env.example .env
# Edit .env with your API keys

# 3. Install and configure Kraken CLI
# https://github.com/krakenfx/kraken-cli/releases
kraken configure

# 4. Install frontend deps
cd frontend && npm install && cd ..

# 5. Start agent + API
pdm run start

# 6. Start dashboard (new terminal)
cd frontend && npm run dev
# Open http://localhost:5173
```

## Backtesting

Run a 3-phase historical backtest using 60 days of 5m candles (signals) + 2 years of 1h candles (regime context) sourced from Yahoo Finance:

```bash
# Standard backtest — rules / confluence+rules / +LLM phases
pdm run backtest

# Disable LLM (faster, no API cost)
python -m backtest.run --balance 10000 --no-llm

# Limit to last N days of 5m history (default uses all ~60 days)
python -m backtest.run --no-llm --days 30

# Walk-forward analysis (out-of-sample windows)
python -m backtest.run --no-llm --walk-forward --wf-train 30 --wf-test 7

# Fee sensitivity sweep (find your break-even fee)
python -m backtest.run --no-llm --fee-sweep

# Custom fee (e.g. Kraken maker rate)
python -m backtest.run --no-llm --fee 0.0016

# Skip AI optimizer (faster, no Gemini API call)
python -m backtest.run --no-llm --no-optimize
```

**Three phases compared side-by-side:**

| Phase | What runs | Purpose |
|---|---|---|
| Phase 1 | Rule engine only | Baseline — how good are the rules alone? |
| Phase 2 | Confluence gate + rules | Production mode — signal quality filter applied |
| Phase 3 | Confluence + rules + Gemini LLM | Full pipeline — validates the LLM adds alpha |

Results are saved as `.xlsx` in `backtest/results/` with a per-trade log, equity curve, and config snapshot.

### AI Strategy Optimizer

After every backtest, the optimizer automatically sends the trade log and current config to Gemini and returns **5 ranked parameter suggestions** — printed to the console and saved as `backtest/results/optimize_PAIR_YYYYMMDD.json`.

Each suggestion includes a rationale and the exact `.env` values to change. To apply one, copy the `changes` block into your `.env` and re-run.

```bash
# Optimizer runs by default — disable with:
python -m backtest.run --no-llm --no-optimize
```

## Configuration

All strategy parameters are configurable via `.env` — no code changes required. Copy `.env.example` to `.env` and edit.

**Core**

| Variable | Description | Default |
|---|---|---|
| `GEMINI_API_KEY` | Google Gemini API key | required |
| `KRAKEN_API_KEY_READONLY` | Read-only key (balance, orders) | required |
| `KRAKEN_API_SECRET_READONLY` | Read-only secret | required |
| `KRAKEN_API_KEY_TRADING` | Trading key (live mode only) | required for live |
| `KRAKEN_API_SECRET_TRADING` | Trading secret (live mode only) | required for live |
| `TRADING_MODE` | `paper` or `live` | `paper` |
| `TRADING_PAIR` | Kraken pair symbol | `BTCUSD` |
| `MAX_POSITION_PCT` | Max portfolio fraction per trade | `0.05` (5%) |
| `CONFIDENCE_THRESHOLD` | Minimum AI confidence to consider a trade | `0.75` |
| `STOP_LOSS_PCT` | Minimum stop-loss floor (ATR-based if larger) | `0.03` (3%) |
| `DAILY_LOSS_LIMIT_PCT` | Circuit breaker threshold | `0.08` (8%) |
| `MAX_OPEN_POSITIONS` | Maximum concurrent open positions | `2` |
| `CONFLUENCE_MIN_SCORE` | Minimum confluence votes to pass gate (0–8) | `3` |
| `FAST_LOOP_MINUTES` | Fast loop interval | `15` |
| `STANDARD_LOOP_MINUTES` | Standard loop interval | `60` |
| `TREND_LOOP_MINUTES` | Trend/regime refresh interval | `240` |
| `API_HOST` | FastAPI bind host | `0.0.0.0` |
| `API_PORT` | FastAPI port | `8000` |
| `CORS_ORIGINS` | Allowed CORS origins | `http://localhost:5173` |

**Strategy — rule thresholds** (tunable without code changes)

| Variable | Description | Default |
|---|---|---|
| `RSI_OVERSOLD` | Rule 1: deep-oversold BUY level | `28` |
| `RSI_OVERBOUGHT` | Rule 2: overbought SELL level | `72` |
| `RSI_SOFT_OVERSOLD` | Rule 7: ranging bounce BUY level | `35` |
| `RSI_SOFT_OVERBOUGHT` | Rule 8: ranging fade SELL level | `65` |
| `RSI_BULL_MIN` / `RSI_BULL_MAX` | Rule 5: RSI range for bullish momentum | `40` / `65` |
| `RSI_BEAR_MIN` / `RSI_BEAR_MAX` | Rule 6: RSI range for bearish momentum | `35` / `60` |
| `ATR_STOP_MULTIPLIER` | Stop distance = ATR × this | `1.5` |
| `TP_RATIO` | Take-profit distance = stop × this (R:R) | `2.0` |
| `ADX_STRONG_THRESHOLD` | ADX above this → TRENDING_STRONG | `35` |
| `ADX_WEAK_THRESHOLD` | ADX above this → TRENDING_WEAK; below → RANGING | `25` |
| `ATR_VOLATILE_ZSCORE` | ATR z-score above this → VOLATILE (stand aside) | `2.0` |
| `MAX_HOLD_HOURS` | Max hours to hold a position before forced exit | `48` |
| `RULE_CONF_EXTREME` | Confidence for Rules 1 & 2 (RSI extreme + BB + MACD) | `0.82` |
| `RULE_CONF_CROSS` | Confidence for Rules 3 & 4 (EMA cross + regime) | `0.78` |
| `RULE_CONF_STATE` | Confidence for Rules 5 & 6 (EMA momentum state) | `0.72` |
| `RULE_CONF_RANGING` | Confidence for Rules 7 & 8 (RSI bounce/fade) | `0.70` |

## Agent Loops

| Loop | Interval | What it does |
|---|---|---|
| **Fast loop** | 15 min | Rule engine only — executes immediately if rule confidence ≥ 0.82. Also checks stop/TP on every tick and saves fast-loop decision records to DB. |
| **Standard loop** | 60 min | Full cycle: fetch multi-TF data → indicators → regime → confluence gate → OFI → Gemini LLM → narrative context → rule engine → consensus → 3-tier risk → execute. Saves portfolio snapshot on every cycle. |
| **Trend loop** | 4 hours | Refreshes 5-state market regime used by all other loops as context. |
| **Shock guard** | Continuous | WebSocket price + order book stream. Emergency closes all positions if price drops 3% in 5 minutes. Accumulates OFI from Level 2 book channel. |

## Decision Pipeline (Standard Loop)

```
1.  Fetch OHLCV (5m · 1h) + live price
2.  Compute 10 indicators (RSI · MACD · BB · VWAP · EMA · ATR · ADX · regime · microstructure · volume profile)
3.  Detect 5-state regime — if VOLATILE → stand aside, save record, exit cycle
4.  Confluence score — if < CONFLUENCE_MIN_SCORE signals agree → HOLD, save record, exit cycle
5.  Read OFI score from shock guard WebSocket buffer
6.  Build Gemini prompt (indicators + sentiment + reflection + signal breakdown)
7.  LLM decision — action + confidence + reasoning
8.  Narrative context — confidence modifier + require_higher_confluence flag
9.  Rule engine decision — deterministic triggers (8 rules)
10. Aggregate — both LLM and rules must agree on direction (consensus)
11. Tier 2 portfolio check — exposure cap, open positions < MAX_OPEN_POSITIONS
12. Tier 3 circuit breaker — halt if daily loss > DAILY_LOSS_LIMIT_PCT
13. Tier 1 per-trade check — confidence ≥ threshold, position sizing, stop/TP calculation
14. Execute via Kraken CLI
15. Save trade + decision + stop/TP + execution quality (slippage) to SQLite
```

## Risk Management

**Tier 1 — Per-Trade Guard**
- Confidence must meet `CONFIDENCE_THRESHOLD`
- Position size = Kelly criterion (half-Kelly) or `MAX_POSITION_PCT × portfolio_value`
- Stop distance = `max(ATR × 1.5, STOP_LOSS_PCT × entry_price)`
- Take-profit = `entry ± stop_distance × 2.0` (enforced 2:1 R:R)

**Tier 2 — Portfolio Guard**
- Max `MAX_OPEN_POSITIONS` concurrent trades
- Max total exposure = 30% of portfolio (computed from open position sizes)

**Tier 3 — Circuit Breaker**
- Activates when daily P&L drops below `-DAILY_LOSS_LIMIT_PCT`
- Blocks all new trades until auto-reset at midnight UTC or manual reset

**Shock Guard**
- Continuous WebSocket price + Level 2 order book monitoring
- If price drops ≥ 3% from its 5-minute high → emergency close all open positions
- Accumulates Order Flow Imbalance (OFI) from bid/ask quantity deltas

## API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/api/health` | GET | Liveness check with uptime |
| `/api/state` | GET | Agent status, last decision, regime, circuit breaker state |
| `/api/trades?limit=50` | GET | Trade history — newest first, includes stop/TP/closed_at |
| `/api/decisions?limit=100` | GET | Decision history with AI reasoning and confluence breakdown |
| `/api/metrics` | GET | All-time portfolio metrics (win rate, Sharpe, drawdown, daily PnL) |
| `/api/price` | GET | Current live BTC price from Kraken |
| `/api/mode` | POST | Switch between paper and live mode at runtime |
| `/docs` | GET | Interactive OpenAPI documentation |

## Architecture

```
backend/
  config/settings.py          Pydantic settings — all env vars validated at startup
  data/fetcher.py             Kraken CLI subprocess wrapper (OHLCV, balance, orders)
  data/sentiment.py           Fear & Greed Index + CoinDesk RSS headlines
  data/market_data.py         Microstructure: bid/ask spread, order book depth
  data/onchain.py             On-chain metrics from Blockchain.info
  indicators/engine.py        10 technical indicators via pandas-ta
  indicators/confluence.py    Bull/bear vote scoring (0–8) with per-signal breakdown
  indicators/regime.py        5-state market regime detection (trending/ranging/volatile)
  indicators/volume_profile.py  Intrabar POC, Value Area High/Low
  brain/gemini.py             Gemini Flash LLM — builds prompt, parses JSON response
  brain/rules.py              Deterministic 8-rule engine (event-based + state-based)
  brain/aggregator.py         Consensus logic (LLM + rules must agree)
  brain/narrative.py          Narrative context — confidence modifier + tail-risk flag
  brain/reflection.py         Trade memory builder — recent trades injected into LLM context
  risk/guard.py               Three-tier risk management + ATR stop/TP calculation
  execution/trader.py         Kraken CLI order placement (paper + live) + slippage tracking
  execution/shock_guard.py    WebSocket flash crash protection + OFI accumulation
  memory/store.py             SQLite3 persistence (decisions, trades, snapshots, state, execution_quality)
  api/app.py                  FastAPI app factory
  api/routes.py               REST endpoints + Pydantic response models
  main.py                     Orchestration — all three agent loops + stop/TP monitor

backtest/
  data.py                     yfinance OHLCV loader with CSV caching (5m: 60d, 1h: 2y)
  run.py                      3-phase backtest runner + walk-forward + fee sweep CLI
  simulator.py                Trade state machine — stop/TP/max-hold exits, P&L, stats
  report.py                   Excel report writer (per-trade log + equity curve)
  optimizer.py                AI optimizer — sends results to Gemini, returns 5 config suggestions

frontend/src/
  pages/DashboardPage.jsx     Main dashboard (status, metrics, price chart)
  pages/TradesPage.jsx        Full-screen trade log
  pages/DecisionsPage.jsx     Decision history with AI reasoning
  components/TradeLog.jsx     Trade table with expanded stop/target/closed-at view
  components/IndicatorPanel   10 confluence signals with bull/bear/neutral breakdown
  components/PriceChart       Live price chart via Recharts
```

## Docker Volumes (persistent data)

| Path on host | Path in container | Contents |
|---|---|---|
| `./logs/` | `/app/logs` | Rotating log files (10 MB, 7 days, gzip) |
| `./backend/data/` | `/app/backend/data` | `trading.db` — all trades, decisions, metrics |
| `~/Library/Application Support/kraken` (macOS) | `/root/.local/share/kraken` | Kraken CLI paper trading balance + state |

## License

MIT
