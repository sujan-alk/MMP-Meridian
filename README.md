# Meridian

A production-ready automated market-making bot for the ALKIMI token, operating simultaneously across KuCoin, Gate.io, MEXC, and Kraken using an async Python architecture with advanced quantitative pricing models, a hierarchical Regime Master agent, and multi-layered risk management.

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Architecture](#architecture)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [API Reference](#api-reference)
- [Safety Systems](#safety-systems)
- [Quantitative Models](#quantitative-models)
- [Database](#database)
- [Deployment](#deployment)
- [Development](#development)

---

## Overview

Meridian continuously quotes two-sided markets (bids and asks) across 4 exchanges, automatically adjusting spread width and order depth based on real-time volatility and market regime. It computes a single **global mid-price** as a weighted average across all exchanges, uses that as the reference for all quotes, and tracks inventory drift to skew orders when the token balance deviates from its starting position.

Key design principles:
- **Dry-run by default** — calculates and logs orders without placing them until `LIVE_MODE=true`
- **Async-first** — every exchange interaction, DB write, and API call is non-blocking
- **Pluggable exchange layer** — CCXT connectors can be swapped for a C++ implementation via the factory pattern
- **Hot-reload config** — `bot.json` parameters can be updated at runtime via the REST API without restarting

---

## Features

- Simultaneous market making on **KuCoin, Gate.io, MEXC, and Kraken**
- **Weighted global mid-price** computed from all active exchanges every second
- **Huy's Meta Config V1** spread engine — power-curve interpolation maps volatility to spread levels
- **Zhang-Zhang (2018) OHLCV volatility estimator** with regime detection (trending up/down/choppy)
- **Inventory-aware skew factor** — automatically increases buy or sell depth when token holdings drift
- **Multi-layer safety**: Q-Switch emergency stop, circuit breaker (daily P&L/drawdown), heartbeat monitor, rate limiter
- **REST + WebSocket API** (FastAPI) for monitoring, control, and real-time event streaming
- **Structured JSON logging** via structlog (coloured in dev, JSON in production)
- **Async SQLite** (aiosqlite) with WAL mode for persistent order, fill, and metrics history
- **RL agent scaffold** — Phase 1 uses the quant model; Phase 2 hooks in a PPO/SAC agent

---

## Architecture

Meridian implements **Option B: Hierarchical Architecture** — a single Regime Master agent provides a unified market regime view to all four exchange bots, eliminating regime divergence across exchanges.

```
main.py
  ├── Database (aiosqlite, WAL mode)
  ├── LiveFeed (WebSocket broadcast hub)
  ├── Orchestrator
  │   ├── Global price loop (1 s tick — fetches all 4 tickers, computes global_mid)
  │   ├── VolatilityEngine  (rolling std-dev + Zhang-Zhang)
  │   ├── AggressivenessModel (power-curve mapping vol → [0.0, 1.0])
  │   ├── RegimeMaster  ← Option B hierarchical architecture
  │   │   ├── HMM RegimeDetector (RANGING / TRENDING / HIGH_VOL / THIN_BOOK)
  │   │   ├── Zhang-Zhang cross-check
  │   │   ├── Reconciliation (disagree → conservative params)
  │   │   └── Broadcasts unified RegimeState to all ExchangeBots
  │   └── ExchangeBot × 4  (one per exchange, each with its own async queue)
  │       ├── InventoryTracker
  │       ├── QSwitch
  │       ├── CircuitBreaker
  │       ├── HeartbeatMonitor
  │       ├── RateLimiter
  │       ├── SpreadEngine  (regime spread_mult applied)
  │       ├── DepthEngine   (regime depth_mult applied)
  │       └── OrderManager (diff-and-repost)
  └── FastAPI server (uvicorn)
      ├── REST routes  (/health, /api/*, /api/regime)
      └── WebSocket    (/ws)
```

### Regime Master (Option B)

The `RegimeMaster` is the top-level intelligence that ensures all 4 exchange bots quote with a **single coherent view** of the market regime:

| Regime | Behaviour |
|---|---|
| `RANGING` | Tight spreads (×1.0), full depth (×1.0), high aggressiveness (0.8) — best for MM |
| `TRENDING` | Wider spreads (×1.5), reduced depth (×0.7), lean inventory with trend |
| `HIGH_VOL` | Very wide spreads (×3.0), minimal depth (×0.3), near-passive (agg=0.1) |
| `THIN_BOOK` | Wide spreads (×2.0), half depth (×0.5), fully passive (agg=0.0) |

When HMM and Zhang-Zhang disagree, conservative parameters are applied automatically.

### Module Map

| Path | Responsibility |
|---|---|
| `main.py` | Entry point; wires up DB, LiveFeed, Orchestrator, and uvicorn |
| `config/schema.py` | Pydantic models for all config params with validation |
| `config/settings.py` | Loads `bot.json` and environment variables at startup and on hot-reload |
| `core/orchestrator.py` | Runs the global 1 s tick loop; distributes `GlobalState` to exchange bots |
| `core/exchange_bot.py` | Per-exchange tick: balance fetch → safety check → grid compute → diff & repost |
| `core/order_manager.py` | Diffs desired order grid against live open orders; cancels/places only what changed |
| `core/inventory_tracker.py` | Tracks balance drift; computes `skew_factor` ∈ [0.5, 2.0] |
| `db/database.py` | aiosqlite connection wrapper with WAL mode |
| `db/migrations.py` | Creates tables: `orders`, `fills`, `inventory_snapshots`, `rl_features` |
| `db/queries.py` | Named async query functions for all DB operations |
| `exchange/base.py` | Abstract `BaseConnector` interface; `Ticker`, `Balance`, `Order`, `Fill` dataclasses |
| `exchange/ccxt_connector.py` | CCXT implementation with retry logic for all 4 exchanges |
| `exchange/factory.py` | `create_connector()` factory — swap CCXT for C++ here |
| `quant/volatility.py` | Rolling std-dev + Zhang-Zhang OHLCV estimator; regime detection |
| `quant/aggressiveness.py` | Maps volatility to aggressiveness [0.0, 1.0] via power-curve |
| `quant/regime_detector.py` | HMM regime detector (RANGING/TRENDING/HIGH_VOL/THIN_BOOK) with Baum-Welch training |
| `quant/spread_engine.py` | Computes bid/ask price levels per the Huy Meta Config V1 model; accepts `spread_mult` |
| `quant/depth_engine.py` | Distributes USD budget across levels (geometric decay blended with equal); accepts `depth_mult` |
| `core/regime_master.py` | RegimeMaster: reconciles HMM + ZZ, broadcasts unified RegimeState to all bots |
| `safety/q_switch.py` | Emergency stop; triggers on low balance or manual API call; sends webhook alert |
| `safety/circuit_breaker.py` | Daily P&L and drawdown tracking; halts trading when thresholds are hit |
| `safety/heartbeat.py` | Detects tick loop stalls; triggers Q-Switch after N missed beats |
| `safety/rate_limiter.py` | Token-bucket limiter to stay within exchange API rate limits |
| `api/app.py` | FastAPI factory; attaches orchestrator, DB, and LiveFeed to `app.state` |
| `api/routes.py` | All REST endpoint handlers |
| `api/models.py` | Pydantic response models |
| `api/websocket.py` | `LiveFeed` WebSocket hub; broadcasts real-time events to all connected clients |
| `agents/base_agent.py` | Abstract `BaseAgent`: `observe(AgentObservation) → AgentAction` |
| `agents/feature_collector.py` | Assembles observation vector; persists RL features to DB |
| `agents/random_agent.py` | Phase 1 placeholder: returns `aggressiveness_override=None` |
| `utils/logging.py` | structlog configuration |
| `utils/math_utils.py` | `power_curve`, `normalize_weights`, `geometric_decay`, `clip`, `pct_change` |
| `utils/time_utils.py` | `now_s()`, `now_ms()`, `RollingWindow` |

---

## Quick Start

### Prerequisites

- Python 3.10+
- API keys for one or more of: KuCoin, Gate.io, MEXC, Kraken

### 1. Clone and install

```bash
git clone <repo-url>
cd Alkimi-MM-Platform
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
# Edit .env with your exchange API keys and desired runtime settings
```

### 3. Review bot configuration

Open `bot.json` and verify the spread, depth, and safety parameters (see [Configuration](#configuration) below). The defaults are conservative and use `"dry_run": true`.

### 4. Run in dry-run mode (safe — no real orders placed)

```bash
python main.py
```

The REST API will be available at `http://localhost:8000`. Check `GET /health` to verify the bot is running.

### 5. Enable live trading

Once you are satisfied with the dry-run output, set `LIVE_MODE=true` in `.env` and restart:

```bash
LIVE_MODE=true python main.py
```

> **Warning:** Live mode places real orders with real funds. Ensure your safety thresholds in `bot.json` are set correctly before proceeding.

---

## Configuration

Configuration is split between environment variables (secrets and runtime flags) and `bot.json` (trading parameters).

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `LIVE_MODE` | `false` | Set to `true` to place real orders |
| `PORT` | `8000` | HTTP API port |
| `DB_PATH` | `data/mm_bot.db` | SQLite database path |
| `BOT_CONFIG_PATH` | `bot.json` | Path to trading config file |
| `LOG_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `ALERT_WEBHOOK_URL` | *(unset)* | Slack/Discord webhook URL for emergency alerts |
| `KUCOIN_API_KEY` | — | KuCoin API key |
| `KUCOIN_API_SECRET` | — | KuCoin API secret |
| `KUCOIN_PASSPHRASE` | — | KuCoin API passphrase (required by KuCoin) |
| `GATE_API_KEY` | — | Gate.io API key |
| `GATE_API_SECRET` | — | Gate.io API secret |
| `MEXC_API_KEY` | — | MEXC API key |
| `MEXC_API_SECRET` | — | MEXC API secret |
| `KRAKEN_API_KEY` | — | Kraken API key |
| `KRAKEN_API_SECRET` | — | Kraken API secret |

### bot.json Reference

```jsonc
{
  // Global dry-run override. If true, no orders are placed regardless of LIVE_MODE.
  "dry_run": true,

  // Weights for the global mid-price calculation. Must sum to 1.0.
  "global_mid_weights": {
    "kucoin": 0.45,
    "gate":   0.45,
    "mexc":   0.05,
    "kraken": 0.05
  },

  // Volatility model parameters (shared across all exchanges)
  "volatility": {
    "window_minutes": 10,     // Rolling window for std-dev calculation
    "low_threshold":  0.001,  // Vol below this → aggressiveness = 1.0 (tightest spreads)
    "high_threshold": 0.003,  // Vol above this → aggressiveness = 0.0 (widest spreads)
    "power":          2.0     // Power-curve exponent for the aggressiveness mapping
  },

  // Per-exchange configuration (one entry per exchange)
  "exchanges": [
    {
      "exchange":        "kucoin",
      "symbol":          "ALKIMI/USDT",
      "quote_currency":  "USDT",
      "enabled":         true,

      // Spread: bid/ask price levels as % from global_mid
      "spread": {
        "buy_min_pct":    -5.0,  // Furthest buy level (agg=0): -5% below mid
        "buy_max_pct":    -0.1,  // Closest buy level (agg=1):  -0.1% below mid
        "sell_min_pct":    0.3,  // Closest sell level (agg=1): +0.3% above mid
        "sell_max_pct":    7.0,  // Furthest sell level (agg=0): +7% above mid
        "curve_strength":  4.0   // Controls curve shape; higher = more aggressive clustering
      },

      // Depth: budget distribution across price levels
      "depth": {
        "levels":            15,      // Number of bid/ask levels per side
        "total_budget_usd": 1000.0,   // Total USD to deploy across all levels
        "curve_strength":    4.0,     // Controls passive vs. equal distribution blend
        "min_order_usd":     5.0      // Minimum order size (smaller orders are skipped)
      },

      // Safety: risk thresholds for this exchange
      "safety": {
        "min_balance_usd":        50.0,  // Q-Switch triggers if USD drops below this
        "min_balance_token":     100.0,  // Q-Switch triggers if token drops below this
        "max_requests_per_second": 8,    // Rate limiter bucket size
        "heartbeat_interval_s":    5.0,  // Expected tick interval in seconds
        "max_missed_heartbeats":   3,    // Q-Switch triggers after this many missed beats
        "max_daily_loss_pct":     10.0,  // Circuit breaker: halt if daily loss > 10%
        "max_drawdown_pct":       15.0   // Circuit breaker: halt if drawdown > 15%
      }
    }
    // ... repeat for gate, mexc, kraken
  ],

  // Initial token/USD balances used as the baseline for inventory skew tracking
  "initial_balances": {
    "kucoin": { "usd": 500.0, "token": 5000.0 },
    "gate":   { "usd": 500.0, "token": 5000.0 },
    "mexc":   { "usd": 250.0, "token": 2500.0 },
    "kraken": { "usd": 250.0, "token": 2500.0 }
  }
}
```

Configuration can be updated at runtime without restarting: `PUT /api/config` with a partial or full `bot.json` body.

---

## API Reference

The FastAPI server runs on `http://localhost:8000` by default. Interactive docs are available at `/docs`.

### Health

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Returns `{status, version, uptime_s}` |

### Status

| Method | Path | Description |
|---|---|---|
| GET | `/api/status` | Overall status: all exchange states, global_mid, volatility, aggressiveness, regime |
| GET | `/api/exchanges/{exchange}` | Per-exchange detail: running, dry_run, safety flags, open orders, balances, skew_factor |

### Orders & Fills

| Method | Path | Query Params | Description |
|---|---|---|---|
| GET | `/api/orders` | `exchange`, `limit` (default 100) | Recent orders from DB |
| GET | `/api/fills` | `exchange`, `limit` (default 200), `since` (unix ts) | Recent fills from DB |

### Balances & Metrics

| Method | Path | Description |
|---|---|---|
| GET | `/api/balances` | All exchange balances with drift percentages and skew factors |
| GET | `/api/metrics` | Aggregate metrics: global_mid, volatility, aggressiveness, fill_rate_1m, pnl_1h, total_open_orders |
| GET | `/api/regime` | Current unified RegimeState: regime, confidence, zz_regime, agreement, mm_params |

### Control

| Method | Path | Description |
|---|---|---|
| POST | `/api/control/pause` | Trigger Q-Switch on all exchanges (cancels all orders) |
| POST | `/api/control/resume` | Reset Q-Switch and circuit breaker on all exchanges |
| POST | `/api/control/emergency_stop` | Immediately cancel all open orders on all exchanges |
| POST | `/api/control/exchanges/{exchange}/pause` | Pause a single exchange |
| POST | `/api/control/exchanges/{exchange}/resume` | Resume a single exchange |

### Config

| Method | Path | Description |
|---|---|---|
| PUT | `/api/config` | Hot-reload trading parameters; accepts full or partial `bot.json` body |

### WebSocket

Connect to `ws://localhost:8000/ws` to receive real-time events:

```json
{
  "event": "tick_update",
  "data": { "exchange": "kucoin", "global_mid": 0.00123, "aggressiveness": 0.72, ... }
}
```

**Event types:**

| Event | Description |
|---|---|
| `tick_update` | Per-exchange tick (price, vol, aggressiveness, open order count) |
| `order_placed` | New order successfully placed |
| `order_filled` | Order fill detected |
| `order_canceled` | Order canceled (stale or on Q-Switch) |
| `inventory_update` | Inventory snapshot with skew factor |
| `aggressiveness_change` | Aggressiveness level changed significantly |
| `emergency_stop` | Q-Switch or circuit breaker triggered |
| `heartbeat` | Periodic keepalive from the bot |
| `config_reloaded` | Hot-reload completed successfully |
| `bot_started` / `bot_stopped` | Exchange bot lifecycle events |

---

## Safety Systems

The bot has four independent safety layers. Any one of them can halt trading independently.

### Q-Switch (Emergency Stop)

Triggers automatically when:
- USD balance on an exchange falls below `min_balance_usd`
- Token balance on an exchange falls below `min_balance_token`
- Triggered manually via `POST /api/control/pause`

On trigger: all open orders on the affected exchange are canceled and an alert is sent to `ALERT_WEBHOOK_URL` (if configured). Trading does **not** resume automatically — a manual `POST /api/control/resume` is required.

### Circuit Breaker

Tracks daily equity (USD + token × global_mid) for each exchange. Resets at midnight UTC.

Triggers when:
- Daily loss exceeds `max_daily_loss_pct` (default 10%)
- Peak-to-trough drawdown exceeds `max_drawdown_pct` (default 15%)

Can be reset manually via `POST /api/control/resume`.

### Heartbeat Monitor

Expects the tick loop to call `beat()` at least once every `heartbeat_interval_s` (default 5 s). If `max_missed_heartbeats` (default 3) consecutive beats are missed (i.e. the loop has stalled for 15+ seconds), the Q-Switch is triggered.

### Rate Limiter

Token-bucket rate limiter per exchange. Default limits:
- KuCoin / Gate.io: 8 requests/second
- MEXC / Kraken: 6 requests/second

Requests that would exceed the limit are queued or dropped to prevent exchange bans.

---

## Quantitative Models

### Global Mid-Price

Every second, tickers are fetched from all enabled exchanges in parallel:

```
global_mid = Σ (weight_i × mid_i)   for all responding exchanges
```

Weights are normalized if any exchange fails to respond, so the global mid always sums to 1.0 across responding sources.

### Volatility

Two estimators run in parallel. The higher estimate is used by default:

1. **Simple rolling std-dev** — standard deviation of log returns over the last `window_minutes` (default 10 min)
2. **Zhang-Zhang (2018) OHLCV estimator** — uses open, high, low, close, and volume from candles; distinguishes three regimes:
   - `trending_up` — mean log(Close/Open) > +0.15%
   - `trending_down` — mean log(Close/Open) < -0.15%
   - `choppy` — otherwise

### Aggressiveness

Maps the current volatility to a scalar `aggressiveness ∈ [0.0, 1.0]` using a power curve:

- `vol ≤ low_threshold` → `aggressiveness = 1.0` (tightest spreads)
- `vol ≥ high_threshold` → `aggressiveness = 0.0` (widest spreads)
- In between: power-curve interpolation with exponent `power` (default 2.0)

### Spread Engine (Huy's Meta Config V1)

For each side (bid/ask) and each level `i ∈ [1, N]`, the price level is:

```
level_pct = min_pct + (max_pct - min_pct) × t^gamma
```

where `t = i / N` and `gamma = exp(curve_strength × (1 - 2 × aggressiveness))`.

- At `aggressiveness = 1.0`: levels cluster near mid (tight, incentivise volume)
- At `aggressiveness = 0.0`: levels cluster far from mid (wide, protect inventory)

### Depth Engine

Total budget is distributed across levels using a blend of two strategies:

- **Passive (geometric decay)**: front-loaded — level 1 gets the most budget, each subsequent level decays by factor `r`
- **Equal distribution**: flat — each level gets `budget / N`

The blend ratio is `aggressiveness ^ curve_strength`. At high aggressiveness the distribution is more equal (more volume at outer levels); at low aggressiveness it is more front-loaded.

The `skew_factor` from InventoryTracker further multiplies buy vs. sell budgets:

```
skew_factor = clip(1.0 - token_drift_pct × 2.0, 0.5, 2.0)
```

- `skew > 1.0` → increase buy budget (bot is short relative to initial position)
- `skew < 1.0` → increase sell budget (bot is long relative to initial position)

### Order Manager (Diff-and-Repost)

On each tick, the OrderManager compares the desired grid against open orders cached in memory:

1. Any open order within `PRICE_TOLERANCE_PCT` (0.05%) of its desired level is **kept**
2. Open orders outside tolerance are **canceled**
3. Missing levels are **placed**

This minimises the number of API calls per tick — only stale orders are touched.

---

## Database

SQLite database (aiosqlite, WAL mode) at `DB_PATH` (default `data/mm_bot.db`).

### Tables

**`orders`**
```sql
id TEXT PRIMARY KEY, exchange TEXT, symbol TEXT, side TEXT,
price REAL, amount REAL, amount_usd REAL, status TEXT,
placed_at REAL, updated_at REAL
```
Status values: `open`, `filled`, `canceled`, `partial`.

**`fills`**
```sql
id TEXT PRIMARY KEY, order_id TEXT, exchange TEXT, symbol TEXT, side TEXT,
filled_price REAL, filled_amount REAL, fee REAL, fee_currency TEXT,
filled_at REAL, pnl_usd REAL
```

**`inventory_snapshots`**
```sql
id INTEGER PRIMARY KEY, exchange TEXT, usd REAL, token REAL,
global_mid REAL, volatility REAL, aggressiveness REAL,
skew_factor REAL, snapshot_at REAL
```

**`rl_features`**
```sql
id INTEGER PRIMARY KEY, timestamp REAL,
vol_simple REAL, vol_zz REAL, zz_regime TEXT,
aggressiveness REAL, global_mid REAL,
buy_spread_l1 REAL, sell_spread_l1 REAL,
skew_factor REAL, fill_rate_1m REAL, pnl_1h REAL
```

---

## Deployment

### Railway (recommended)

The repo includes `railway.json` and `Procfile` for zero-config Railway deployment.

1. Push the repo to GitHub
2. Create a new Railway project from the repo
3. Set all environment variables from `.env.example` in the Railway dashboard
4. Railway will detect the `Procfile` and run `python main.py`
5. The `/health` endpoint is used as the healthcheck

### Manual / Docker

```bash
# Set environment variables
export LIVE_MODE=false
export PORT=8000
export DB_PATH=/data/mm_bot.db
# ... set exchange keys ...

python main.py
```

For Docker, mount a volume at `/data` to persist the SQLite database across container restarts.

---

## Development

### Project setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Running tests

```bash
pytest
pytest -v --asyncio-mode=auto   # verbose, with async test support
```

> Note: test files are not yet included in the repo. The pytest infrastructure (`pytest-asyncio`) is wired up and ready.

### Extending the exchange layer

To add a new exchange or swap CCXT for a native C++ connector:

1. Implement `BaseConnector` from `exchange/base.py`
2. Register the new connector in `exchange/factory.py`
3. Add credentials to `.env.example` and `config/settings.py`
4. Add an entry in `bot.json` under `exchanges`

### Plugging in an RL agent

The `agents/` directory provides the scaffold for a reinforcement-learning agent:

1. Subclass `BaseAgent` (`agents/base_agent.py`)
2. Implement `observe(AgentObservation) → AgentAction`
3. Set `aggressiveness_override` in the returned `AgentAction` to override the quant model
4. Pass the agent instance to `Orchestrator` at startup in `main.py`

The `rl_features` table accumulates the observation vectors needed for offline training.

### Logging

Structured JSON logging in production; coloured key-value output in development.

```bash
LOG_LEVEL=DEBUG python main.py   # verbose output including every tick
```

### Hot-reloading config

While the bot is running, update trading parameters without restarting:

```bash
curl -X PUT http://localhost:8000/api/config \
  -H "Content-Type: application/json" \
  -d '{"exchanges": [{"exchange": "kucoin", "depth": {"total_budget_usd": 2000}}]}'
```
