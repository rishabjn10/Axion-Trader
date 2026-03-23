# axion-trader

Autonomous AI trading agent for Kraken exchange — Gemini Flash LLM + deterministic rules + three-tier risk management.

> **⚠️ Trading Risk Disclaimer**
> This software is for educational and research purposes. Cryptocurrency trading carries significant financial risk — you can lose your entire investment. Past simulated (paper) performance is not indicative of future real results. This is not financial advice. Never trade with money you cannot afford to lose. The authors accept no liability for financial losses incurred through use of this software.

```
                       ┌─────────────────────────────────────────────────┐
                       │                  axion-trader                   │
                       └─────────────────────────────────────────────────┘

 ┌──────────────┐   ┌──────────────┐   ┌────────────────────┐   ┌─────────────┐
 │  Kraken CLI  │──▶│  Indicators  │──▶│     AI Brain       │──▶│    Risk     │
 │  OHLCV 15m   │   │  8 signals   │   │  Gemini Flash LLM  │   │  3-tier     │
 │  OHLCV  1h   │   │  confluence  │   │  + Rule Engine     │   │  guard      │
 │  OHLCV  4h   │   │  regime      │   │  + Reflection      │   │             │
 └──────────────┘   └──────────────┘   └────────────────────┘   └─────────────┘
        │                                                               │
 ┌──────────────┐                                             ┌─────────────────┐
 │  Sentiment   │                                             │   Execution     │
 │  F&G + news  │                                             │  paper / live   │
 └──────────────┘                                             └────────┬────────┘
                                                                       │
 ┌─────────────────────────────────────────────────────────────────────▼────────┐
 │                          SQLite  (trading.db)                                │
 │   decisions · trades · portfolio_snapshots · agent_state                    │
 └─────────────────────────────────────────────────────────────────────┬────────┘
                                                                       │
 ┌─────────────────────────────────────────────────────────────────────▼────────┐
 │                        FastAPI REST API  (:8000)                             │
 │   /api/state · /api/trades · /api/metrics · /api/decisions · /api/price     │
 └─────────────────────────────────────────────────────────────────────┬────────┘
                                                                       │
 ┌─────────────────────────────────────────────────────────────────────▼────────┐
 │                       React Dashboard  (:3000)                               │
 │    Agent Status · PnL Cards · Trade Log · Price Chart · Confluence Panel     │
 └──────────────────────────────────────────────────────────────────────────────┘
```

## Features

- **Multi-timeframe analysis** — 15m, 1h, and 4h OHLCV from Kraken
- **8 confluence signals** — RSI, MACD cross, Bollinger Bands %B, VWAP, EMA cross, ATR, ADX, market regime; each votes bull/bear; score gates the AI cycle
- **Hybrid AI brain** — Gemini Flash LLM (strategic context) + deterministic rule engine (tactical triggers); both must reach consensus to execute
- **LLM memory / reflection** — recent trade history injected into every Gemini prompt so the AI reasons about what it has already done
- **Three-tier risk management** — per-trade sizing, portfolio exposure cap, circuit breaker
- **ATR-based stop-loss with floor** — `max(ATR×1.5, STOP_LOSS_PCT × entry)` so the configured percentage acts as a minimum guarantee
- **2:1 R:R enforcement** — take-profit is always 2× the actual stop distance
- **Stop/TP monitoring** — checked every fast loop tick; auto-closes positions when triggered
- **Flash crash protection** — WebSocket shock guard: 3% drop in 5 min triggers emergency close of all positions
- **Full DB audit trail** — every cycle type writes a record: fast loop evaluations, circuit breaker skips, regime changes, portfolio snapshots on every standard cycle
- **Confluence signal breakdown** — per-signal bull/bear/neutral stored in DB, returned by API, displayed visually on dashboard, injected into Gemini prompt
- **Paper / Live modes** — full simulation via `kraken paper` commands; zero code changes required to switch
- **React dashboard** — real-time status, PnL metrics, trade log with stop/target/closed-at, indicator panel with signal breakdown
- **Docker support** — `docker compose up -d` for 24/7 background operation

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

## Configuration

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
| `FAST_LOOP_MINUTES` | Fast loop interval | `15` |
| `STANDARD_LOOP_MINUTES` | Standard loop interval | `60` |
| `TREND_LOOP_MINUTES` | Trend/regime refresh interval | `240` |
| `API_HOST` | FastAPI bind host | `0.0.0.0` |
| `API_PORT` | FastAPI port | `8000` |
| `CORS_ORIGINS` | Allowed CORS origins | `http://localhost:5173` |

## Agent Loops

| Loop | Interval | What it does |
|---|---|---|
| **Fast loop** | 15 min | Rule engine only — executes immediately if rule confidence ≥ 0.82. Also checks stop/TP on every tick and saves fast-loop decision records to DB. |
| **Standard loop** | 60 min | Full cycle: fetch multi-TF data → indicators → confluence gate (≥4/8) → Gemini LLM → rule engine → consensus → 3-tier risk → execute. Saves portfolio snapshot on every cycle regardless of trade. |
| **Trend loop** | 4 hours | Refreshes 4h regime (trending/ranging) used by all other loops as context. |
| **Shock guard** | Continuous | WebSocket price stream. Emergency closes all positions if price drops 3% in 5 minutes. |

## Decision Pipeline (Standard Loop)

```
1. Fetch OHLCV (15m · 1h · 4h) + live price
2. Compute 8 indicators (RSI · MACD · BB · VWAP · EMA · ATR · ADX · regime)
3. Confluence score — if < 4/8 signals agree → HOLD, save record, exit cycle
4. Build Gemini prompt (indicators + sentiment + reflection + signal breakdown)
5. LLM decision — action + confidence + reasoning
6. Rule engine decision — deterministic triggers
7. Aggregate — both must agree on direction (consensus)
8. Tier 2 portfolio check — exposure < 15%, open positions < MAX_OPEN_POSITIONS
9. Tier 3 circuit breaker — halt if daily loss > DAILY_LOSS_LIMIT_PCT
10. Tier 1 per-trade check — confidence ≥ threshold, position sizing, stop/TP calculation
11. Execute via Kraken CLI
12. Save trade + decision + stop/TP to SQLite
```

## Risk Management

**Tier 1 — Per-Trade Guard**
- Confidence must meet `CONFIDENCE_THRESHOLD`
- Position size = `MAX_POSITION_PCT × portfolio_value`
- Stop distance = `max(ATR × 1.5, STOP_LOSS_PCT × entry_price)`
- Take-profit = `entry ± stop_distance × 2.0` (enforced 2:1 R:R)

**Tier 2 — Portfolio Guard**
- Max `MAX_OPEN_POSITIONS` concurrent trades (counted from SQLite, not Kraken CLI — reliable in paper mode)
- Max total exposure = 15% of portfolio (computed as `sum(volume × entry_price) / portfolio_value`)

**Tier 3 — Circuit Breaker**
- Activates when daily P&L drops below `-DAILY_LOSS_LIMIT_PCT`
- Blocks all new trades until auto-reset at midnight UTC or manual reset

**Shock Guard**
- Continuous WebSocket price monitoring
- If price drops ≥ 3% from its 5-minute high → emergency close all open positions

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
  config/settings.py        Pydantic settings — all env vars validated at startup
  data/fetcher.py            Kraken CLI subprocess wrapper (OHLCV, balance, orders)
  data/sentiment.py          Fear & Greed Index + CoinDesk RSS headlines
  data/onchain.py            On-chain metrics from Blockchain.info
  indicators/engine.py       8 technical indicators via pandas-ta
  indicators/confluence.py   Bull/bear vote scoring (0–8) with per-signal breakdown
  indicators/regime.py       4h market regime detection (trending / ranging)
  brain/gemini.py            Gemini Flash LLM — builds prompt, parses JSON response
  brain/rules.py             Deterministic 4-rule engine
  brain/aggregator.py        Consensus logic (LLM + rules must agree)
  brain/reflection.py        Trade memory builder — recent trades injected into LLM context
  risk/guard.py              Three-tier risk management + ATR stop/TP calculation
  execution/trader.py        Kraken CLI order placement (paper + live)
  execution/shock_guard.py   WebSocket flash crash protection
  memory/store.py            SQLite3 persistence (decisions, trades, snapshots, state)
  api/app.py                 FastAPI app factory
  api/routes.py              REST endpoints + Pydantic response models
  main.py                    Orchestration — all three agent loops + stop/TP monitor

frontend/src/
  pages/DashboardPage.jsx    Main dashboard (status, metrics, price chart)
  pages/TradesPage.jsx       Full-screen trade log
  pages/DecisionsPage.jsx    Decision history with AI reasoning
  components/TradeLog.jsx    Trade table with expanded stop/target/closed-at view
  components/IndicatorPanel  8 confluence signals with bull/bear/neutral breakdown
  components/PriceChart      Live price chart via Recharts
```

## Docker Volumes (persistent data)

| Path on host | Path in container | Contents |
|---|---|---|
| `./logs/` | `/app/logs` | Rotating log files (10 MB, 7 days, gzip) |
| `./backend/data/` | `/app/backend/data` | `trading.db` — all trades, decisions, metrics |
| `kraken_state` (named volume) | `/root/.config/kraken` | Kraken CLI paper trading balance |

## License

MIT
