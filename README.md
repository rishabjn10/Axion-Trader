# axion-trader

Autonomous AI trading agent for Kraken exchange — Gemini Flash + deterministic rules + three-tier risk management.

```
                        ┌─────────────────────────────────────────────────┐
                        │                  axion-trader                   │
                        └─────────────────────────────────────────────────┘

  ┌─────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
  │ Kraken CLI  │───▶│  Indicators  │───▶│  AI Brain    │───▶│    Risk      │
  │  (OHLCV)    │    │  (8 signals) │    │  Gemini +    │    │  3-tier      │
  │  (ticker)   │    │  confluence  │    │  Rule Engine │    │  guard       │
  └─────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
         │                                                           │
  ┌─────────────┐                                           ┌──────────────┐
  │  Sentiment  │                                           │  Execution   │
  │  F&G + news │                                           │  paper/live  │
  └─────────────┘                                           └──────────────┘
                                                                    │
  ┌─────────────────────────────────────────────────────────────────▼──────┐
  │                          SQLite (trading.db)                           │
  │     decisions · trades · portfolio_snapshots · agent_state             │
  └─────────────────────────────────────────────────────────────────┬──────┘
                                                                    │
  ┌─────────────────────────────────────────────────────────────────▼──────┐
  │                     FastAPI REST API (:8000)                           │
  │   /api/state · /api/trades · /api/metrics · /api/decisions             │
  └─────────────────────────────────────────────────────────────────┬──────┘
                                                                    │
  ┌─────────────────────────────────────────────────────────────────▼──────┐
  │                     React Dashboard (:5173)                            │
  │    Agent Status · PnL Cards · Trade Log · Price Chart · Indicators     │
  └───────────────────────────────────────────────────────────────────────┘
```

## Features

- **Multi-timeframe analysis**: Fetches 15m, 1h, and 4h OHLCV from Kraken
- **8 technical indicators**: RSI, MACD, Bollinger Bands, VWAP, ATR, EMA 9/21, ADX
- **Hybrid AI brain**: Google Gemini 2.0 Flash (strategic) + deterministic rules (tactical)
- **Consensus aggregation**: Both brain systems must agree to execute a trade
- **Three-tier risk management**: Per-trade, portfolio, and circuit breaker guards
- **Flash crash protection**: WebSocket shock guard with 3% emergency exit
- **Paper/Live modes**: Full simulation with `kraken --paper` flag
- **Langtrace observability**: Every Gemini call is instrumented and traced
- **React dashboard**: Real-time monitoring with PnL, metrics, trade log, and indicators

## Prerequisites

- Python 3.13+
- [PDM](https://pdm-project.org/) package manager
- Node.js 20+
- [Kraken CLI](https://github.com/krakenfx/kraken-cli)
- Kraken account with API keys
- Google Gemini API key
- Langtrace API key (optional — for observability)

## Quick Start

```bash
# 1. Install PDM
pip install pdm

# 2. Clone repo
git clone https://github.com/YOUR_USERNAME/axion-trader
cd axion-trader

# 3. Install Python dependencies
pdm install

# 4. Configure environment
cp .env.example .env
# Edit .env with your API keys

# 5. Install Kraken CLI
# See: https://github.com/krakenfx/kraken-cli

# 6. Configure Kraken CLI
kraken configure

# 7. Verify Kraken CLI works
kraken ticker BTCUSD -o json

# 8. Install frontend dependencies
cd frontend && npm install && cd ..

# 9. Start the agent + API server
pdm run start

# 10. Start the React dashboard (new terminal)
cd frontend && npm run dev

# 11. Open dashboard
# http://localhost:5173
```

## Configuration

All configuration is in `.env`. Copy `.env.example` to `.env` and fill in:

| Variable | Description | Default |
|---|---|---|
| `GEMINI_API_KEY` | Google Gemini API key | required |
| `KRAKEN_API_KEY_READONLY` | Kraken read-only key | required |
| `KRAKEN_API_SECRET_READONLY` | Kraken read-only secret | required |
| `KRAKEN_API_KEY_TRADING` | Kraken trading key | required for live |
| `KRAKEN_API_SECRET_TRADING` | Kraken trading secret | required for live |
| `LANGTRACE_API_KEY` | Langtrace observability key | required |
| `TRADING_MODE` | `paper` or `live` | `paper` |
| `TRADING_PAIR` | Kraken pair symbol | `BTCUSD` |
| `MAX_POSITION_PCT` | Max portfolio fraction per trade | `0.05` (5%) |
| `CONFIDENCE_THRESHOLD` | Minimum AI confidence | `0.75` |
| `STOP_LOSS_PCT` | Stop-loss distance | `0.03` (3%) |
| `DAILY_LOSS_LIMIT_PCT` | Circuit breaker threshold | `0.08` (8%) |
| `MAX_OPEN_POSITIONS` | Maximum concurrent positions | `2` |
| `API_HOST` | FastAPI bind host | `0.0.0.0` |
| `API_PORT` | FastAPI bind port | `8000` |
| `CORS_ORIGINS` | Allowed CORS origins | `http://localhost:5173` |

## API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/api/health` | GET | Liveness check with uptime |
| `/api/state` | GET | Agent status, last decision, circuit breaker |
| `/api/trades?limit=50` | GET | Trade history (newest first) |
| `/api/decisions?limit=100` | GET | Decision history with AI reasoning |
| `/api/metrics` | GET | Portfolio performance metrics |
| `/api/price` | GET | Current live price from Kraken |
| `/api/mode` | POST | Switch paper/live mode |
| `/docs` | GET | Interactive OpenAPI documentation |

## Trading Modes

### Paper Mode (`TRADING_MODE=paper`)
- All orders use `kraken --paper order add ...`
- Kraken CLI simulates execution with real market prices
- No real money involved
- Dashboard shows **PAPER** badge in amber
- Logs prefix every trade with `[PAPER]`

### Live Mode (`TRADING_MODE=live`)
- All orders use `kraken order add ...`
- Real orders placed on Kraken exchange
- Real money execution
- Dashboard shows **LIVE** badge in green
- Logs prefix every trade with `[LIVE]`
- Requires `KRAKEN_API_KEY_TRADING` and `KRAKEN_API_SECRET_TRADING`

## Agent Loops

| Loop | Interval | Description |
|---|---|---|
| Fast loop | 15 minutes | Rule engine only — immediate execution if confidence ≥ 0.82 |
| Standard loop | 60 minutes | Full cycle: Gemini + rules + all risk checks |
| Trend loop | 4 hours | Regime refresh from 4h chart |
| Shock guard | Continuous | WebSocket price monitoring for flash crashes |

## Risk Management

**Tier 1 — Per-Trade Guard**: Checks confidence threshold, position sizing (max 5% of portfolio), and enforces 2:1 minimum risk/reward using ATR-based stops.

**Tier 2 — Portfolio Guard**: Enforces max open positions (2) and max total exposure (15%).

**Tier 3 — Circuit Breaker**: Halts ALL trading when daily losses exceed 8%. Auto-resets at midnight UTC.

**Shock Guard**: Monitors WebSocket prices. If price drops 3% from its 5-minute high, emergency closes all positions.

## CLI Flags

```bash
pdm run start               # Full system (agent + API)
pdm run start -- --paper    # Force paper mode
pdm run start -- --live     # Force live mode
pdm run agent               # Agent only (no API)
pdm run api                 # API only (no agent)
```

## Architecture

```
backend/
  config/settings.py        Pydantic settings — all env vars validated at startup
  data/fetcher.py            Kraken CLI subprocess wrapper with retry logic
  data/sentiment.py          Fear & Greed Index + CoinDesk RSS news
  data/onchain.py            On-chain metrics from Blockchain.info
  indicators/engine.py       pandas-ta-classic: all 8 indicators
  indicators/confluence.py   Signal confluence scoring (0–8)
  indicators/regime.py       Market regime detection (trending/ranging)
  brain/gemini.py            Gemini 2.0 Flash with Langtrace instrumentation
  brain/rules.py             Deterministic 4-rule engine
  brain/aggregator.py        Consensus logic (both must agree)
  brain/reflection.py        Trade memory for LLM context
  risk/guard.py              Three-tier risk management
  execution/trader.py        Kraken CLI order placement
  execution/shock_guard.py   WebSocket flash crash protection
  memory/store.py            SQLite3 persistence layer
  api/app.py                 FastAPI app factory
  api/routes.py              All 7 API endpoints
  main.py                    Orchestration + agent loops
```

## License

MIT
