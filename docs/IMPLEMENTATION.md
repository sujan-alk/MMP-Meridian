# ALKIMI Market Making Platform — Implementation Document

**Version:** 1.0
**Date:** 2026-04-01
**Token:** ALKIMI
**Exchanges:** KuCoin, Gate.io, MEXC, Kraken
**Codebase:** ~6,400 lines Python | 62 unit tests

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [System Architecture](#2-system-architecture)
3. [Quant Model — Huy's Meta Config V1](#3-quant-model--huys-meta-config-v1)
4. [Exchange Integration](#4-exchange-integration)
5. [Order Management](#5-order-management)
6. [Risk and Safety](#6-risk-and-safety)
7. [Data Layer](#7-data-layer)
8. [API and Monitoring](#8-api-and-monitoring)
9. [Configuration](#9-configuration)
10. [Deployment](#10-deployment)
11. [Testing](#11-testing)
12. [Roadmap and Known Issues](#12-roadmap-and-known-issues)

---

## 1. Executive Summary

The ALKIMI MM Platform is an automated market-making bot that maintains continuous liquidity across four centralised exchanges simultaneously. It places and manages limit orders on both sides of the order book for the ALKIMI token, adjusting spread width, order depth, and inventory bias in real time based on market volatility.

### Key Characteristics

- **Multi-exchange:** KuCoin, Gate.io, MEXC, Kraken — all managed from a single async Python process
- **Volatility-driven:** Spread and depth parameters react automatically to changing market conditions via a configurable aggressiveness model
- **Inventory-aware:** Skew factor biases buy/sell budgets to rebalance token holdings toward a neutral position
- **Safety-first:** Four independent safety layers (Q-Switch, circuit breaker, heartbeat monitor, rate limiter) can each independently halt trading
- **Observable:** REST API, WebSocket live feed, structured logging, and a persistent SQLite database for order, fill, and inventory history
- **Hot-reloadable:** Most trading parameters can be updated via API without restarting the bot

### Technology Stack

| Layer | Technology |
|-------|-----------|
| Runtime | Python 3.11+, asyncio |
| Exchange connectivity | CCXT (async) |
| Web server | FastAPI + uvicorn |
| Database | aiosqlite (SQLite in WAL mode) |
| Validation | Pydantic v2 |
| Numerics | NumPy |
| Logging | structlog (JSON in production) |
| Deployment | Railway.app |

---

## 2. System Architecture

### 2.1 Process Model

The bot runs as a single Python process with concurrent asyncio coroutines. Python's GIL is not a bottleneck because all work is I/O-bound (exchange API calls, database writes, WebSocket broadcasts). Four cooperative coroutines sharing one event loop is more predictable and debuggable than threads or subprocesses.

### 2.2 Startup Flow

```
main.py
  |
  +-- Load bot.json via Pydantic (BotConfig)
  +-- Load .env via pydantic-settings (ExchangeSecrets)
  +-- Initialise aiosqlite database (WAL mode, create schema)
  +-- Create LiveFeed (WebSocket broadcaster)
  +-- Create Orchestrator
  |     +-- VolatilityEngine (shared, global)
  |     +-- AggressivenessModel (shared, global)
  |     +-- 4x ExchangeBot (one per enabled exchange)
  |           +-- CCXTConnector (via factory)
  |           +-- InventoryTracker
  |           +-- SpreadEngine
  |           +-- DepthEngine
  |           +-- OrderManager (with RateLimiter)
  |           +-- QSwitch
  |           +-- CircuitBreaker
  |           +-- Heartbeat
  |
  +-- asyncio.gather(
  |     orchestrator.start(),    # price loop + bot loops
  |     uvicorn.serve(app),      # FastAPI server on PORT
  |     shutdown_handler()       # SIGTERM/SIGINT graceful stop
  |   )
```

### 2.3 Module Dependency Graph

```
main.py
  +-- config/ (schema, settings)
  +-- db/ (database, queries, migrations)
  +-- api/ (app, routes, models, metrics, websocket)
  +-- core/
        +-- orchestrator --> quant/ (volatility, aggressiveness)
        |                --> exchange/ (factory -> ccxt_connector -> base)
        |                --> db/
        |                --> api/ (websocket)
        +-- exchange_bot --> quant/ (spread, depth, aggressiveness)
        |                --> safety/ (q_switch, heartbeat, rate_limiter, circuit_breaker)
        |                --> agents/ (feature_collector)
        |                --> db/
        +-- order_manager --> exchange/ (base)
        |                 --> safety/ (rate_limiter)
        |                 --> db/
        +-- inventory_tracker --> exchange/ (Balance dataclass)
```

### 2.4 Concurrency Model

The Orchestrator runs a global price loop at ~1-second intervals. After computing the global mid-price, volatility, and aggressiveness, it broadcasts a `GlobalState` dataclass to each ExchangeBot via per-bot `asyncio.Queue` instances (maxsize=5, `put_nowait`). If a bot's queue is full (it's behind), the tick is silently dropped — this is correct for a price feed where you always want the latest value.

Each ExchangeBot has its own `_run_loop()` coroutine that awaits the next GlobalState, then runs a full tick cycle: fetch balance, safety checks, quant model, order management, fill polling, and database writes. All four bots run concurrently but independently.

### 2.5 Directory Structure

```
MM Bot_Python/
+-- main.py                       # Entry point
+-- bot.json                      # Trading configuration
+-- requirements.txt              # Dependencies
+-- .env.example                  # Environment variable template
+-- config/
|   +-- schema.py                 # Pydantic validation models
|   +-- settings.py               # Env/file loader
+-- core/
|   +-- orchestrator.py           # Global coordinator
|   +-- exchange_bot.py           # Per-exchange trading loop
|   +-- order_manager.py          # Diff-and-repost logic
|   +-- inventory_tracker.py      # Position and skew management
|   +-- rebalancer.py             # Rebalance utility
+-- quant/
|   +-- volatility.py             # Vol estimation + Zhang-Zhang
|   +-- aggressiveness.py         # Vol-to-agg mapping
|   +-- spread_engine.py          # Price level computation
|   +-- depth_engine.py           # Order size distribution
+-- exchange/
|   +-- base.py                   # Abstract connector interface
|   +-- ccxt_connector.py         # CCXT implementation
|   +-- factory.py                # Factory pattern
+-- safety/
|   +-- q_switch.py               # Emergency stop
|   +-- circuit_breaker.py        # Daily P&L limits
|   +-- heartbeat.py              # Stall detection
|   +-- rate_limiter.py           # API rate throttle
+-- db/
|   +-- database.py               # aiosqlite wrapper
|   +-- migrations.py             # Schema definition
|   +-- queries.py                # Async DB operations
+-- api/
|   +-- app.py                    # FastAPI factory
|   +-- routes.py                 # Endpoint handlers
|   +-- models.py                 # Response DTOs
|   +-- metrics.py                # Aggregation helpers
|   +-- websocket.py              # LiveFeed broadcaster
+-- agents/
|   +-- base_agent.py             # Abstract RL agent interface
|   +-- feature_collector.py      # Observation vector assembly
|   +-- random_agent.py           # Phase 1 placeholder
+-- utils/
|   +-- logging.py                # Structlog config
|   +-- math_utils.py             # Pure math functions
|   +-- time_utils.py             # Time helpers
+-- tests/                        # 9 test files, 62 tests
+-- scripts/                      # Utility scripts
+-- docs/                         # Documentation
```

---

## 3. Quant Model — Huy's Meta Config V1

The quant model answers two questions on every tick:

1. **Where to place orders?** SpreadEngine: power-curve distribution of bid/ask spreads
2. **How much to place?** DepthEngine: passive/equal blend of USD amounts per level

Both are driven by a single scalar — **aggressiveness** in [0, 1] — derived from rolling market volatility.

### 3.1 Volatility Estimation

#### Simple Rolling Volatility (Primary — Phase 1)

```
window:    10 minutes of price observations (sampled each second)
returns[i] = (price[i] - price[i-1]) / price[i-1]
volatility = std(returns)
```

- Source: `quant/volatility.py` -> `VolatilityEngine.rolling_vol()`
- Window configurable via `bot.json` -> `volatility.window_minutes`
- Returns 0.0 until at least 2 observations (warm-up)

#### Zhang-Zhang (2018) OHLCV Volatility (Phase 1b)

A more robust estimator using intraday high/low data from 1-minute candles:

```
For each candle (O, H, L, C) in log prices:
  hl_term = 0.5 * (ln(H/L))^2
  co_term = (2*ln(2) - 1) * (ln(C/O))^2

zz_variance = mean(hl_term - co_term)
zz_vol      = sqrt(max(zz_variance, 0))
```

The `ln(H/L)` term captures intraday range; the correction term removes drift bias, making the estimator accurate even in trending markets.

#### Regime Detection

```
net_direction = mean(ln(C/O))   across the candle window

regime = "trending_up"    if net_direction >  0.0005
regime = "trending_down"  if net_direction < -0.0005
regime = "choppy"         otherwise
```

Regime feeds into per-side aggressiveness adjustments (see 3.2).

### 3.2 Aggressiveness Model

Converts volatility into a single control parameter in [0, 1].

```
vol <= low_threshold (0.001)   ->  aggressiveness = 1.0   (fully aggressive)
vol >= high_threshold (0.003)  ->  aggressiveness = 0.0   (fully passive)
between:
  aggressiveness = 1 - ((vol - low_threshold) / (high_threshold - low_threshold)) ^ power
```

Default `power = 2.0` (quadratic). Configurable via `bot.json` -> `volatility.power`.

| vol    | aggressiveness | Interpretation           |
|--------|----------------|--------------------------|
| 0.0010 | 1.00           | Ultra-calm: trade hard   |
| 0.0015 | 0.94           | Quiet market             |
| 0.0020 | 0.75           | Moderate volatility      |
| 0.0025 | 0.44           | Elevated volatility      |
| 0.0030 | 0.00           | High vol: protect positions |

**Regime-Aware Aggressiveness (Phase 1b):**

When Zhang-Zhang regime data is available, aggressiveness is split per side:

| Regime       | Buy aggressiveness | Sell aggressiveness |
|------------- |-------------------|---------------------|
| trending_up  | base * 0.8        | base * 1.1          |
| trending_down| base * 1.1        | base * 0.8          |
| choppy       | base              | base                |

This causes the bot to accumulate on dips (trending_down -> more aggressive buying) and distribute on rallies (trending_up -> more aggressive selling).

### 3.3 Spread Engine

Computes n price levels per side using power-curve interpolation between tightest and widest configured spreads.

**Formula:**

```
t     = linspace(0, 1, n_levels)           # [0, 1/n, 2/n, ..., 1]
gamma = exp(curve_strength * (2*agg - 1))

spread[i] = tightest + (widest - tightest) * t[i]^gamma
```

**Gamma behaviour:**

| agg | gamma (curve_strength=4) | Effect                             |
|-----|--------------------------|-------------------------------------|
| 1.0 | exp(-4) = 0.018          | t^0.018 ~ 1 -> levels cluster near tightest (aggressive) |
| 0.5 | exp(0) = 1.0             | Linear spacing (balanced)           |
| 0.0 | exp(4) = 54.6            | t^54.6 ~ 0 -> levels cluster near widest (passive) |

**Default Spread Ranges (per exchange, configurable):**

| Side | Tightest (closest to mid) | Widest (furthest from mid) |
|------|---------------------------|----------------------------|
| Buy  | -0.1%                     | -5.0%                      |
| Sell | +0.3%                     | +7.0%                      |

**Conversion to prices:**

```
buy_price[i]  = global_mid * (1 + buy_spread[i] / 100)
sell_price[i] = global_mid * (1 + sell_spread[i] / 100)
```

**Worked Example — Sell side (n=5, sell_min=0.3%, sell_max=7.0%):**

```
agg = 1.0 (aggressive):
  Levels: +0.30%, +0.30%, +0.30%, +0.30%, +0.31%
  -> All orders cluster just above mid

agg = 0.5 (balanced):
  Levels: +0.30%, +2.02%, +3.75%, +5.47%, +7.00%
  -> Even distribution across full range

agg = 0.0 (passive):
  Levels: +6.80%, +6.93%, +6.98%, +7.00%, +7.00%
  -> All orders cluster at widest spread
```

### 3.4 Depth Engine

Distributes the total USD budget across order levels, blending two strategies based on aggressiveness.

**Anchor distributions (normalised to sum to 1.0):**

```
passive = geometric_decay(n, ratio=0.8)    # [0.35, 0.28, 0.22, ...]  (front-loaded)
equal   = [1/n, 1/n, ..., 1/n]            # uniform
```

**Blend formula:**

```
blend = aggressiveness ^ curve_strength    # 0^4=0, 0.5^4=0.0625, 1^4=1

proportions = (1 - blend) * passive + blend * equal
```

| agg | blend (curve_strength=4) | Distribution              |
|-----|--------------------------|---------------------------|
| 0.0 | 0.000                    | 100% passive (front-loaded: inner levels get most capital) |
| 0.5 | 0.063                    | 94% passive, 6% equal     |
| 1.0 | 1.000                    | 100% equal (uniform across all levels) |

**Budget per side:**

```
half_budget = total_budget_usd / 2

buy_budget  = half_budget * clip(skew_factor, 0.5, 2.0)
sell_budget = half_budget * clip(2.0 - skew_factor, 0.5, 2.0)

amount_usd[i] = proportions[i] * side_budget
amount_token[i] = amount_usd[i] / price[i]
```

**Minimum order enforcement:** Each level must meet `min_order_usd` (default 5.0). If the total exceeds the side budget after applying minimums, amounts are scaled down uniformly.

### 3.5 Inventory Skew

The InventoryTracker monitors balance drift from initial positions and produces a skew factor that biases buy/sell budgets.

```
token_drift = (current_token - initial_token) / initial_token
raw_skew    = 1.0 - (token_drift * 2.0)
skew_factor = clip(raw_skew, 0.5, 2.0)
```

| Token drift | Skew factor | Effect                              |
|-------------|-------------|-------------------------------------|
| -20% (over-token)  | 0.60 | Reduce buy budget, increase sell    |
| 0% (neutral)       | 1.00 | Equal buy/sell budget               |
| +10% (under-token) | 1.20 | Increase buy budget                 |
| +25% (under-token) | 1.50 | Significantly more buying           |

This creates a natural mean-reversion force on inventory: as the bot accumulates too many tokens, it reduces buy-side depth and increases sell-side depth, and vice versa.

### 3.6 Order Grid Construction

Each tick, the full desired state of the order book is computed:

```
For each side (buy / sell):
  spreads[i]      = SpreadEngine.compute_levels(agg, n)
  amounts_usd[i]  = DepthEngine.compute_amounts(agg, n, skew, side)
  price[i]        = global_mid * (1 + spreads[i] / 100)
  amount_token[i] = amounts_usd[i] / price[i]
```

This produces an `OrderGrid` dataclass containing 15 buy prices + amounts and 15 sell prices + amounts, which is passed to the OrderManager for execution.

---

## 4. Exchange Integration

### 4.1 Abstract Interface

All exchange interaction is mediated through `BaseConnector` (ABC):

```python
class BaseConnector(ABC):
    async def connect() -> None
    async def disconnect() -> None
    async def fetch_ticker() -> Ticker
    async def fetch_candles(timeframe, limit) -> list[Candle]
    async def fetch_balance() -> Balance
    async def create_limit_order(side, price, amount) -> Order
    async def cancel_order(order_id) -> None
    async def cancel_all_orders() -> None
    async def fetch_open_orders() -> list[Order]
    async def fetch_fills(since_ts, limit) -> list[Fill]
```

### 4.2 CCXT Implementation

`CCXTConnector` wraps `ccxt.async_support` for all four exchanges:

- **Retry logic:** Exponential backoff (3 retries, base delay 1s) on `NetworkError`, `RequestTimeout`, `DDoSProtection`, `RateLimitExceeded`
- **Non-retryable:** `AuthenticationError` fails immediately
- **Exchange quirks:** KuCoin 3-part auth (key + secret + passphrase), Kraken uses `ALKIMI/USD` not `ALKIMI/USDT`, per-exchange rate limits

### 4.3 Factory Pattern

```python
def create_connector(exchange_name, symbol, credentials, quote_currency, ccxt_options):
    # Returns CCXTConnector (default)
    # Swappable for C++ implementation without changing upper layers
```

Only `exchange/factory.py` imports concrete implementations. All callers depend on `BaseConnector`. When C++ connectors are ready, only the factory changes — no other module is touched.

### 4.4 Data Models

```python
Ticker(bid, ask, mid, last, timestamp)
Candle(timestamp, open, high, low, close, volume)
Balance(usd, token, quote_currency)
Order(id, exchange, symbol, side, price, amount, amount_usd, status, timestamp, ...)
Fill(id, order_id, exchange, symbol, side, filled_price, filled_amount, fee, fee_currency, timestamp)
```

### 4.5 Supported Exchanges

| Exchange | CCXT ID  | Symbol        | Quote | Auth                    | Rate limit |
|----------|----------|---------------|-------|-------------------------|------------|
| KuCoin   | kucoin   | ALKIMI/USDT   | USDT  | key + secret + passphrase | 8 req/s   |
| Gate.io  | gate     | ALKIMI/USDT   | USDT  | key + secret              | 8 req/s   |
| MEXC     | mexc     | ALKIMI/USDT   | USDT  | key + secret              | 6 req/s   |
| Kraken   | kraken   | ALKIMI/USD    | USD   | key + secret              | 6 req/s   |

---

## 5. Order Management

### 5.1 Diff-and-Repost Strategy

A naive approach would cancel all orders and re-place them on every tick. Across 4 exchanges with 15 levels per side, that's 120 cancel API calls per second. Instead, `OrderManager.diff_and_repost()` compares the desired grid against current open orders using a price tolerance:

```
PRICE_TOLERANCE_PCT = 0.05%  (5 basis points)

For each open order:
  Find closest desired price on the same side
  If |open_price - desired_price| / desired_price > 0.0005:
    -> Cancel it (price has moved too far)
  Else:
    -> Keep it (within tolerance)

For each desired level not covered by an existing order:
  -> Place a new order
```

In a stable market, this can mean zero cancellations per tick. In normal conditions, 2-4 cancels per tick versus 60+ with a cancel-all approach.

### 5.2 Order Lifecycle

1. **Place:** `create_limit_order()` — simulated in dry-run (fake `DRY-{uuid}` IDs), real on live
2. **Track:** In-memory dict `_open_orders[id]` synced with exchange via `fetch_open_orders()` each tick
3. **Age out:** Orders older than `max_order_age_seconds` (300s) are auto-cancelled
4. **Cancel:** On repricing, Q-Switch trigger, or emergency stop
5. **Fill:** Detected via `fetch_fills()` polling (every 5s), ingested to database

### 5.3 Dry-Run Mode

Two independent guards prevent accidental live trading:

1. `bot.json` -> `dry_run: true` — calculates grids but never calls exchange write APIs
2. `LIVE_MODE=false` (env) — even if dry_run is false, real orders are not placed

**Both must be set to their live values to place real orders.** Additionally, when `LIVE_MODE=true`, the bot requires a `.enable_live_mode` gate file to exist in the working directory as a manual confirmation step.

---

## 6. Risk and Safety

Four independent safety layers operate in parallel. Any single layer can halt trading on an exchange.

### 6.1 Q-Switch (Emergency Stop)

**Trigger conditions:**
- USD balance < `min_balance_usd` (per-exchange, default 50)
- Token balance < `min_balance_token` (per-exchange, default 100)
- Manual API call: `POST /api/control/pause` or `POST /api/control/emergency_stop`

**On trigger:**
1. Sets an `asyncio.Event` (all waiting coroutines notified)
2. Cancels all open orders on the affected exchange
3. Sends webhook alert (Slack/Discord) if `ALERT_WEBHOOK_URL` is configured
4. Halts the exchange bot — requires manual API reset to resume

### 6.2 Circuit Breaker (Daily P&L Limits)

```
equity = usd + token * global_mid
```

Tracks starting equity and peak equity per calendar day (UTC). Halts if:
- Daily loss > `max_daily_loss_pct` (default 10%)
- Drawdown from peak > `max_drawdown_pct` (default 15%)

Resets automatically at UTC midnight or manually via `POST /api/control/reset_circuit_breaker`.

### 6.3 Heartbeat Monitor (Stall Detection)

Each ExchangeBot calls `heartbeat.beat()` on every successful tick. A background task monitors the interval:

- Expected: beat every `heartbeat_interval_s` (default 5s)
- Timeout: 1.5x interval with no beat -> increment missed counter
- Failure: `max_missed_heartbeats` (default 3) consecutive misses -> trigger Q-Switch + cancel all orders

Detects tick loop stalls (network hang, infinite loop, deadlock).

### 6.4 Rate Limiter (API Throttle)

Token-bucket algorithm with per-exchange limits:

- KuCoin/Gate: 8 req/s
- MEXC/Kraken: 6 req/s

Async — blocks callers when the bucket is exhausted, refills continuously. Operates alongside CCXT's built-in rate limiting as a second layer.

### 6.5 Safety Architecture Diagram

```
ExchangeBot._tick()
  |
  +-- QSwitch.check(balance)
  |     balance.usd < min_balance_usd  OR
  |     balance.token < min_balance_token
  |     -> cancel_all_orders() -> webhook alert -> halt
  |
  +-- CircuitBreaker.record_equity(usd, token, mid)
  |     daily_loss > max_daily_loss_pct  OR
  |     drawdown from peak > max_drawdown_pct
  |     -> halt (auto-resets at UTC midnight)
  |
  +-- Heartbeat (background task)
        if > 3 consecutive missed beats (each 5s):
        -> trigger QSwitch -> cancel_all_orders()
        -> emit EMERGENCY_STOP event
```

---

## 7. Data Layer

### 7.1 Technology

- **Engine:** aiosqlite (async wrapper around SQLite)
- **Mode:** WAL (Write-Ahead Logging) for concurrent reads during writes
- **Location:** `data/mm_bot.db` (configurable via `DB_PATH` env var)

### 7.2 Schema

**orders** — placed orders

| Column     | Type | Description                                     |
|------------|------|-------------------------------------------------|
| id         | TEXT PK | Exchange order ID (or `DRY-{uuid}` in dry-run) |
| exchange   | TEXT | Exchange name                                    |
| symbol     | TEXT | Trading pair                                     |
| side       | TEXT | `buy` or `sell`                                  |
| price      | REAL | Limit price                                      |
| amount     | REAL | Token amount                                     |
| amount_usd | REAL | USD value at placement                           |
| status     | TEXT | `open`, `filled`, `canceled`, `partial`          |
| placed_at  | REAL | Unix timestamp                                   |
| updated_at | REAL | Unix timestamp                                   |

**fills** — executed trades

| Column        | Type | Description                        |
|---------------|------|------------------------------------|
| id            | TEXT PK | Fill ID                         |
| order_id      | TEXT | Parent order ID                    |
| exchange      | TEXT | Exchange name                      |
| symbol        | TEXT | Trading pair                       |
| side          | TEXT | `buy` or `sell`                    |
| filled_price  | REAL | Execution price                    |
| filled_amount | REAL | Token amount filled                |
| fee           | REAL | Fee charged                        |
| fee_currency  | TEXT | Fee denomination                   |
| filled_at     | REAL | Unix timestamp                     |
| pnl_usd       | REAL | Realised P&L (nullable)           |

**inventory_snapshots** — balance records (every 30s)

| Column         | Type | Description                      |
|----------------|------|----------------------------------|
| id             | INTEGER AUTOINCREMENT | Row ID          |
| exchange       | TEXT | Exchange name                     |
| usd            | REAL | USD balance                       |
| token          | REAL | Token balance                     |
| global_mid     | REAL | Mid-price at snapshot time        |
| volatility     | REAL | Current volatility                |
| aggressiveness | REAL | Current aggressiveness            |
| skew_factor    | REAL | Inventory skew factor             |
| snapshot_at    | REAL | Unix timestamp                    |

**rl_features** — observation vectors for RL training (every 10s)

| Column          | Type | Description                     |
|-----------------|------|---------------------------------|
| id              | INTEGER AUTOINCREMENT | Row ID         |
| timestamp       | REAL | Unix timestamp                   |
| vol_simple      | REAL | Rolling standard deviation vol   |
| vol_zz          | REAL | Zhang-Zhang volatility           |
| zz_regime       | TEXT | `trending_up`, `trending_down`, `choppy` |
| aggressiveness  | REAL | Current aggressiveness           |
| global_mid      | REAL | Current mid-price                |
| buy_spread_l1   | REAL | Tightest buy spread %            |
| sell_spread_l1  | REAL | Tightest sell spread %           |
| skew_factor     | REAL | Inventory skew factor            |
| fill_rate_1m    | REAL | Fills per minute                 |
| pnl_1h          | REAL | P&L over last hour               |

The `rl_features` table is populated from day 1, building a dataset for Phase 2 RL agent training.

### 7.3 Write Patterns

All database writes are non-blocking (`await db.execute()`). Key write frequencies:

| Event               | Frequency       |
|---------------------|-----------------|
| Order placed/cancelled | On change (per tick if grid changes) |
| Fill ingested       | Every 5s polling cycle |
| Inventory snapshot  | Every 30s        |
| RL features         | Every 10s        |

---

## 8. API and Monitoring

### 8.1 REST API

The bot exposes a FastAPI server (default port 8000). Interactive docs at `/docs`.

**Health and Status:**

| Endpoint                         | Method | Description                        |
|----------------------------------|--------|------------------------------------|
| `/health`                        | GET    | Liveness check (used by Railway)   |
| `/api/status`                    | GET    | All exchanges + global metrics     |
| `/api/exchanges/{exchange}`      | GET    | Single exchange status             |

**Data:**

| Endpoint                         | Method | Description                        |
|----------------------------------|--------|------------------------------------|
| `/api/orders`                    | GET    | Open orders (filterable by exchange, side) |
| `/api/fills`                     | GET    | Fill history (paginated, filterable) |
| `/api/balances`                  | GET    | Per-exchange balances + inventory drift |
| `/api/metrics`                   | GET    | Fill rate, P&L, open order count   |

**Control (requires `MM_API_KEY` header if configured):**

| Endpoint                                    | Method | Description                        |
|---------------------------------------------|--------|------------------------------------|
| `/api/control/pause`                        | POST   | Pause all bots                     |
| `/api/control/resume`                       | POST   | Resume all bots                    |
| `/api/control/emergency_stop`               | POST   | Cancel all orders, halt all bots   |
| `/api/control/exchanges/{exchange}/pause`   | POST   | Pause a single exchange            |
| `/api/control/exchanges/{exchange}/resume`  | POST   | Resume a single exchange           |
| `/api/control/reset_circuit_breaker`        | POST   | Reset circuit breaker              |

**Configuration:**

| Endpoint                                  | Method | Description                        |
|-------------------------------------------|--------|------------------------------------|
| `/api/config`                             | GET    | Current bot.json content           |
| `/api/config`                             | PUT    | Hot-reload full config             |
| `/api/config/exchanges/{exchange}`        | PUT    | Update single exchange config      |

### 8.2 WebSocket Live Feed

Connect to `ws://host:8000/ws` for real-time events. All events follow this envelope:

```json
{
  "event": "event_type_name",
  "data": { ... },
  "timestamp": 1743340800.0
}
```

**Event Types:**

| Event                  | Frequency       | Description                               |
|------------------------|-----------------|-------------------------------------------|
| `tick_update`          | ~1s per exchange | Volatility, agg, skew, orders placed/cancelled |
| `order_placed`         | On placement     | Order ID, side, price, amount              |
| `order_filled`         | On fill detection | Fill details + fee                        |
| `order_canceled`       | On cancellation  | Order ID + reason                         |
| `inventory_update`     | Every 30s        | Balance, skew factor, drift %             |
| `aggressiveness_change`| On 10% threshold | Previous/current agg, vol, direction      |
| `emergency_stop`       | On trigger       | Exchange, reason, trigger source          |
| `heartbeat`            | Every 30s idle   | Server keepalive                          |
| `config_reloaded`      | On PUT /api/config | Changed fields                          |
| `bot_started`          | On start         | Exchange name                             |
| `bot_stopped`          | On stop          | Exchange name                             |

### 8.3 Authentication

Control and config endpoints support optional API key authentication via the `X-API-Key` header. Set the `MM_API_KEY` environment variable to enable. When unset, endpoints are open (suitable for internal/Railway deployment).

---

## 9. Configuration

### 9.1 Two-Layer System

| Layer       | File          | Contents                        | Committed |
|-------------|---------------|---------------------------------|-----------|
| Non-secret  | `bot.json`    | All trading parameters          | Yes       |
| Secrets     | `.env`        | API keys, runtime flags         | No (.gitignored) |

### 9.2 bot.json Structure

```json
{
  "dry_run": true,
  "global_mid_weights": {
    "kucoin": 0.45, "gate": 0.45, "mexc": 0.05, "kraken": 0.05
  },
  "volatility": {
    "window_minutes": 10,
    "low_threshold": 0.001,
    "high_threshold": 0.003,
    "power": 2.0
  },
  "exchanges": [
    {
      "exchange": "kucoin",
      "symbol": "ALKIMI/USDT",
      "quote_currency": "USDT",
      "enabled": true,
      "spread": {
        "buy_min_pct": -0.1,
        "buy_max_pct": -5.0,
        "sell_min_pct": 0.3,
        "sell_max_pct": 7.0,
        "curve_strength": 4.0
      },
      "depth": {
        "levels": 15,
        "total_budget_usd": 1000.0,
        "curve_strength": 4.0,
        "min_order_usd": 5.0
      },
      "safety": {
        "min_balance_usd": 50.0,
        "min_balance_token": 100.0,
        "max_requests_per_second": 8,
        "heartbeat_interval_s": 5.0,
        "max_missed_heartbeats": 3,
        "max_daily_loss_pct": 10.0,
        "max_drawdown_pct": 15.0
      }
    }
  ],
  "initial_balances": {
    "kucoin": { "usd": 500.0, "token": 5000.0 },
    "gate":   { "usd": 500.0, "token": 5000.0 },
    "mexc":   { "usd": 250.0, "token": 2500.0 },
    "kraken": { "usd": 250.0, "token": 2500.0 }
  }
}
```

**Total capital allocated:** $1,500 USD + 15,000 ALKIMI tokens across all exchanges.

### 9.3 Environment Variables

| Variable               | Default         | Description                                |
|------------------------|-----------------|--------------------------------------------|
| `KUCOIN_API_KEY`       | (required)      | KuCoin API key                             |
| `KUCOIN_API_SECRET`    | (required)      | KuCoin API secret                          |
| `KUCOIN_PASSPHRASE`    | (required)      | KuCoin API passphrase (unique to KuCoin)   |
| `GATE_API_KEY`         | (required)      | Gate.io API key                            |
| `GATE_API_SECRET`      | (required)      | Gate.io API secret                         |
| `MEXC_API_KEY`         | (required)      | MEXC API key                               |
| `MEXC_API_SECRET`      | (required)      | MEXC API secret                            |
| `KRAKEN_API_KEY`       | (required)      | Kraken API key                             |
| `KRAKEN_API_SECRET`    | (required)      | Kraken API secret                          |
| `LIVE_MODE`            | `false`         | Enable real order placement                |
| `PORT`                 | `8000`          | FastAPI server port                        |
| `DB_PATH`              | `data/mm_bot.db`| SQLite database path                       |
| `BOT_CONFIG_PATH`      | `bot.json`      | Path to bot configuration file             |
| `LOG_LEVEL`            | `INFO`          | Logging level (DEBUG/INFO/WARNING/ERROR)   |
| `ALERT_WEBHOOK_URL`    | (empty)         | Slack/Discord webhook for Q-Switch alerts  |
| `MM_API_KEY`           | (empty)         | Optional API key for control endpoints     |

### 9.4 Hot-Reload vs Restart

| Parameter                  | Hot-reload | Notes                                  |
|----------------------------|------------|----------------------------------------|
| Spread config              | Yes        | Takes effect next tick                 |
| Depth config               | Yes        | Takes effect next tick                 |
| Volatility thresholds      | Yes        | Takes effect next tick                 |
| Global mid weights         | Yes        | Takes effect next price loop           |
| Safety thresholds          | Yes        | Takes effect next safety check         |
| Initial balances           | Yes        | Takes effect on next balance fetch (~10s) |
| `dry_run`                  | No         | Requires restart (safety guard)        |
| `LIVE_MODE`                | No         | Requires restart (environment variable)|
| `max_requests_per_second`  | No         | Rate limiter initialised at startup    |
| API keys                   | No         | Requires restart                       |

### 9.5 Validation

All configuration is validated by Pydantic v2 models on load:

- `GlobalMidWeights`: weights must sum to 1.0
- `SpreadConfig`: `buy_max_pct < buy_min_pct < 0 < sell_min_pct < sell_max_pct`
- `DepthConfig`: levels in [1, 50], total_budget_usd > 0
- `VolatilityConfig`: low_threshold < high_threshold
- `SafetyConfig`: all thresholds > 0, max_daily_loss_pct in (0, 100]

Invalid config prevents startup or returns HTTP 422 on hot-reload.

---

## 10. Deployment

### 10.1 Platform

The bot is deployed on **Railway.app** with:

- Nixpacks build (auto-detects Python, installs `requirements.txt`)
- Start command: `python main.py`
- Health check: `GET /health` every 30s
- Restart policy: on failure, up to 5 retries
- Persistent volume at `/app/data` for SQLite database

### 10.2 Resource Requirements

| Resource | Recommended | Notes                                       |
|----------|-------------|---------------------------------------------|
| RAM      | 512 MB      | Python asyncio is lightweight; 256 MB minimum |
| CPU      | 0.5 vCPU    | I/O-bound; minimal CPU needed               |
| Disk     | 1 GB volume | SQLite DB grows ~10 MB/day with rl_features |
| Network  | Unlimited   | 4 exchanges * ~1 req/s each                 |

### 10.3 Go-Live Checklist

- [ ] All 4 exchanges show `"running": true` in `/api/status`
- [ ] `tick_update` events flowing in WebSocket
- [ ] Exchange balances in `/api/balances` match actual exchange balances
- [ ] No `ERROR` logs in the last 24 hours
- [ ] `ALERT_WEBHOOK_URL` is set for Q-Switch alerts
- [ ] `min_balance_usd` and `min_balance_token` thresholds are correctly set
- [ ] `initial_balances` in bot.json match current actual balances
- [ ] 24+ hours of dry-run monitoring completed
- [ ] `.enable_live_mode` gate file created
- [ ] `LIVE_MODE=true` set in Railway variables
- [ ] `dry_run: false` committed and deployed

### 10.4 Operational Commands

```bash
# Pause all trading
curl -X POST https://your-app.railway.app/api/control/pause

# Resume all trading
curl -X POST https://your-app.railway.app/api/control/resume

# Emergency stop (cancel all orders immediately)
curl -X POST https://your-app.railway.app/api/control/emergency_stop

# Hot-reload config
curl -X PUT https://your-app.railway.app/api/config \
  -H "Content-Type: application/json" \
  -d @bot.json

# Monitor via WebSocket
wscat -c wss://your-app.railway.app/ws
```

---

## 11. Testing

### 11.1 Framework

pytest + pytest-asyncio, with 62 tests across 9 test files.

### 11.2 Test Coverage

| Module              | Test File                   | What's Tested                              |
|---------------------|-----------------------------|--------------------------------------------|
| SpreadEngine        | test_spread_engine.py       | Power-curve levels, gamma behaviour, edge cases |
| DepthEngine         | test_depth_engine.py        | Budget distribution, geometric decay, min order |
| VolatilityEngine    | test_volatility.py          | Rolling vol, Zhang-Zhang, regime detection |
| AggressivenessModel | test_aggressiveness.py      | Vol-to-agg mapping, thresholds, power curves |
| InventoryTracker    | test_inventory_tracker.py   | Balance drift, skew factor, position limits |
| OrderManager        | test_order_manager.py       | Diff-and-repost, tolerance matching, cancellation |
| ExchangeBot         | test_exchange_bot.py        | Full tick cycle, safety integration         |
| CircuitBreaker      | test_circuit_breaker.py     | Daily loss, drawdown, midnight reset        |

### 11.3 Fixtures

Shared fixtures in `conftest.py` provide:

- `make_ticker()`, `make_balance()`, `make_order()`, `make_candle()` — data model factories
- `spread_config`, `depth_config`, `volatility_config`, `safety_config` — pre-built configs
- `db` — in-memory SQLite database
- `mock_connector` — fully mocked BaseConnector
- `mock_rate_limiter` — no-op rate limiter

### 11.4 Running Tests

```bash
python -m pytest tests/ -v
```

---

## 12. Roadmap and Known Issues

### 12.1 Known Issues

**SpreadEngine gamma formula inversion:** The gamma formula `exp(curve_strength * (1 - 2*agg))` may be inverted relative to the module docstring. At agg=1 (low vol), levels should cluster tight, but depending on parameterisation, the behaviour may be the opposite of stated intent. Flagged in `test_spread_engine.py` with a NOTE. Awaiting confirmation before changing.

### 12.2 Remaining Work (Pre-Production)

| Task                          | Priority | Status         |
|-------------------------------|----------|----------------|
| 24h+ dry-run monitoring       | Critical | Not started    |
| Live exchange validation (1 ALKIMI test order per exchange) | Critical | Not started |
| Alembic database migrations   | Medium   | Not started    |
| Rate limiter audit (dual limiter: app RateLimiter + CCXT enableRateLimit) | Medium | Not started |

### 12.3 Phase 2: RL Agent

The `agents/` module and `rl_features` table are scaffolded for Phase 2 reinforcement learning:

- **Feature collector** writes an observation vector every 10 seconds from day 1
- **PassthroughAgent** (Phase 1) returns `aggressiveness_override = None` (no intervention)
- **Phase 2 target:** Train a PPO/SAC agent on the collected dataset to learn optimal aggressiveness policy, replacing the static volatility-to-aggressiveness mapping

**Observation vector (10 features):**

| Feature           | Description                          |
|-------------------|--------------------------------------|
| vol_simple        | Current rolling volatility           |
| vol_zz            | Zhang-Zhang volatility               |
| zz_regime         | Regime (trending_up/down/choppy)     |
| aggressiveness    | Current aggressiveness scalar        |
| skew_factor       | Inventory skew factor                |
| token_drift_pct   | % drift from initial token balance   |
| global_mid        | Current ALKIMI price                 |
| bid_ask_spread_bps| Tightest level spread in basis points|
| fill_rate_1m      | Fills per minute (rolling)           |
| pnl_1h            | P&L change over last hour            |

---

## Appendix A: Global Mid-Price Formula

```
global_mid = 0.45 * KuCoin_mid + 0.45 * Gate_mid + 0.05 * MEXC_mid + 0.05 * Kraken_mid
```

Weights reflect liquidity depth. KuCoin and Gate.io are the primary price discovery venues for ALKIMI.

**Fallback:** If an exchange fails to respond, weights are normalised over responding exchanges. If Gate (0.45) is down, KuCoin becomes ~0.818, MEXC ~0.091, Kraken ~0.091.

## Appendix B: One-Tick Data Flow

```
1. Orchestrator._price_loop() fires (~1s interval)
   -> Fetch tickers from all 4 exchanges in parallel
   -> Compute weighted global_mid
   -> Update VolatilityEngine (rolling std dev)
   -> Every 60s: fetch candles, compute Zhang-Zhang vol + regime
   -> Compute aggressiveness from vol
   -> Create GlobalState(global_mid, vol, agg, zz_vol, zz_regime, ts)
   -> put_nowait(state) to each bot's asyncio.Queue

2. ExchangeBot._tick(state) fires
   -> Fetch balance (cached; re-fetched if >10s stale)
   -> QSwitch: balance below threshold? -> halt
   -> CircuitBreaker: daily loss/drawdown exceeded? -> halt
   -> InventoryTracker: compute skew_factor from balance drift
   -> SpreadEngine: compute 15 buy + 15 sell spread percentages
   -> DepthEngine: compute 15 buy + 15 sell USD amounts (with skew)
   -> Convert USD amounts to token amounts at current price
   -> OrderManager.diff_and_repost(grid):
       -> Sync open orders from exchange (rate-limited)
       -> Find stale orders (price >0.05% off target)
       -> Cancel stale, place new
       -> Update DB
   -> Every 5s: poll fills -> db.insert_fill()
   -> Every 30s: write inventory_snapshot to DB
   -> Every 10s: write rl_features to DB
   -> Emit TICK_UPDATE to WebSocket clients
   -> Call heartbeat.beat()

3. FastAPI serves /ws clients the emitted events in real-time
```
