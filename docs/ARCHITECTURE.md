# Architecture

## Overview

The bot is a single Python process with four concurrent `asyncio` coroutines — one per exchange — coordinated by a central `Orchestrator`.

```
┌────────────────────────────────────────────────────────────────────────┐
│  main.py                                                                │
│  asyncio.gather(orchestrator.start(), uvicorn.serve(app))              │
└──────────────────────┬─────────────────────────────────────────────────┘
                       │
                       ▼
┌────────────────────────────────────────────────────────────────────────┐
│  Orchestrator  (core/orchestrator.py)                                   │
│                                                                         │
│  _price_loop() [every 1s]                                               │
│  ├── Parallel fetch: KuCoin, Gate, MEXC, Kraken tickers                │
│  ├── global_mid = 0.45×Gate + 0.45×KuCoin + 0.05×MEXC + 0.05×Kraken  │
│  ├── VolatilityEngine.update(global_mid)                                │
│  ├── vol = rolling_vol()                                                │
│  ├── agg = AggressivenessModel.compute(vol)                             │
│  └── → push GlobalState to 4 asyncio.Queue instances (put_nowait)      │
│                                                                         │
│  Config hot-reload via trigger_config_reload(new_config)               │
└──┬─────────────────────────────────────────────────────────────────────┘
   │  asyncio.Queue per bot (drop if full — always want latest price)
   │
   ├──────────────┬──────────────┬──────────────────────────────────────┐
   ▼              ▼              ▼                                      ▼
ExchangeBot    ExchangeBot    ExchangeBot                         ExchangeBot
 (KuCoin)       (Gate)         (MEXC)                              (Kraken)
   │
   ▼  ExchangeBot._tick() [on each GlobalState received]
   ├── fetch_balance (cached 10s)
   ├── QSwitch.check() + CircuitBreaker.check()
   ├── InventoryTracker.update() → skew_factor
   ├── SpreadEngine.compute_levels(agg, n) → buy/sell spread %s
   ├── DepthEngine.compute_amounts(agg, n, skew) → USD amounts
   ├── Convert USD amounts → token amounts at current price
   ├── OrderManager.diff_and_repost(grid)
   │   ├── fetch_open_orders() (sync in-memory cache)
   │   ├── diff: find stale orders (price >0.05% off target)
   │   ├── cancel stale orders only
   │   └── place new/repriced levels only
   ├── poll_fills() → db.insert_fill()
   ├── FeatureCollector.collect() → db.rl_features (every 10s)
   ├── db.insert_inventory_snapshot() (every 30s)
   └── LiveFeed.emit_tick() → WebSocket broadcast

┌──────────────────────────────────────────────────────────────────────┐
│  FastAPI Server  (api/)                                               │
│  REST endpoints + WebSocket /ws                                       │
│  PUT /api/config → validates, saves bot.json, hot-reloads            │
└──────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│  SQLite Database  (db/)                                              │
│  Tables: orders, fills, inventory_snapshots, rl_features             │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Key Design Decisions

### Single asyncio process (not multiprocess)

Python's GIL is not a concern because all bottlenecks are network I/O (exchange API calls). Four cooperative coroutines sharing one event loop is more predictable and debuggable than threads or subprocesses.

### Queue with `put_nowait` + drop

When the Orchestrator distributes a new `GlobalState`, it uses `put_nowait` with `maxsize=5`. If a bot's queue is full (it's behind), the tick is silently dropped. This is correct behaviour for a price feed — you always want the *latest* price, never a stale one from 3 seconds ago.

### Diff-and-repost (not cancel-all)

A naive implementation would cancel all orders and re-place them on every tick. Across 4 exchanges with 15 levels per side, that's 4 × 30 = 120 API calls per second just for cancellations. Instead, `OrderManager.diff_and_repost()` only cancels orders whose price has moved more than `PRICE_TOLERANCE_PCT = 0.05%` from the desired grid level. In a stable market, this can mean zero cancellations per tick.

### C++ swap boundary at `exchange/base.py`

Every caller (`ExchangeBot`, `OrderManager`) receives a `BaseConnector` via dependency injection. Only `exchange/factory.py` imports concrete implementations. When C++ connectors are ready, only the factory changes.

### Global price fallback

If one or more exchanges fail to return a ticker, `_tick_prices()` normalises the weights over the exchanges that *did* respond. If Gate (weight 0.45) is down, KuCoin's weight is re-normalised to ~0.9 of the available total. The bot continues running.

### `dry_run` vs `LIVE_MODE`

There are two separate guards:

1. **`bot.json` → `dry_run: true`** — The bot calculates order grids and simulates orders in memory but calls no exchange write APIs.
2. **`LIVE_MODE=false`** (env) — If `true`, the `Orchestrator` passes `live_mode=True` to each `ExchangeBot`, which passes it to `OrderManager`.

Both must be enabled to place real orders. This double-guard prevents accidental live trading.

---

## Module Dependency Graph

```
main.py
  ├── config/ (schema, settings)
  ├── db/ (database, queries)
  ├── api/ (app, routes, websocket)
  └── core/
        ├── orchestrator ──► quant/ (volatility, aggressiveness)
        │                 ──► exchange/ (factory → ccxt_connector → base)
        │                 ──► db/
        │                 ──► api/ (websocket)
        ├── exchange_bot ──► quant/ (spread, depth, aggressiveness)
        │                ──► safety/ (q_switch, heartbeat, rate_limiter, circuit_breaker)
        │                ──► agents/ (feature_collector)
        │                ──► db/
        ├── order_manager ──► exchange/ (base)
        │                 ──► safety/ (rate_limiter)
        │                 ──► db/
        └── inventory_tracker ──► exchange/ (Balance)
```

---

## Data Flow: One Tick

```
1. Orchestrator._price_loop() fires (every ~1s)
   → Fetches tickers from all 4 exchanges in parallel
   → Computes weighted global_mid
   → Updates VolatilityEngine (rolling std dev)
   → Computes aggressiveness from vol
   → Creates GlobalState dataclass
   → put_nowait(state) to each bot's asyncio.Queue

2. ExchangeBot._tick(state) fires
   → Fetches balance (cached; re-fetched if >10s stale)
   → QSwitch: balance below threshold? → halt
   → CircuitBreaker: daily loss/drawdown exceeded? → halt
   → InventoryTracker: compute skew_factor from balance drift
   → SpreadEngine: compute 15 buy + 15 sell spread percentages
   → DepthEngine: compute 15 buy + 15 sell USD amounts (with skew)
   → Convert USD amounts to token amounts at current price
   → OrderManager.diff_and_repost(grid):
       → Sync open orders from exchange (rate-limited)
       → Find stale orders (price >0.05% off target)
       → Cancel stale, place new
       → Update DB
   → Every 30s: write inventory_snapshot to DB
   → Every 10s: write rl_features to DB
   → Always: emit TICK_UPDATE to WebSocket clients

3. FastAPI serves /ws clients the emitted events in real-time
```

---

## Safety Architecture

```
ExchangeBot._tick()
  │
  ├─ QSwitch.check(balance) ──► triggered?
  │     balance.usd < min_balance_usd  OR
  │     balance.token < min_balance_token
  │     ──► cancel_all_orders()
  │     ──► send webhook alert
  │     ──► halt (requires API reset to resume)
  │
  ├─ CircuitBreaker.record_equity(usd, token, mid)
  │     daily_loss > max_daily_loss_pct  OR
  │     drawdown from peak > max_drawdown_pct
  │     ──► halt (auto-resets at UTC midnight, or via API)
  │
  └─ Heartbeat (background task)
        if > 3 consecutive missed beats (each beat = 5s):
        ──► trigger QSwitch manually
        ──► cancel_all_orders()
        ──► emit EMERGENCY_STOP event
```

---

## Database Schema

```sql
orders (
  id TEXT PK, exchange, symbol, side, price, amount, amount_usd,
  status ("open"|"filled"|"canceled"|"partial"),
  placed_at REAL, updated_at REAL
)

fills (
  id TEXT PK, order_id, exchange, symbol, side,
  filled_price, filled_amount, fee, fee_currency,
  filled_at REAL, pnl_usd REAL  -- nullable
)

inventory_snapshots (
  id INTEGER AUTOINCREMENT, exchange, usd, token,
  global_mid, volatility, aggressiveness, skew_factor,
  snapshot_at REAL
)

rl_features (
  id INTEGER AUTOINCREMENT, timestamp REAL,
  vol_simple, vol_zz, zz_regime,
  aggressiveness, global_mid,
  buy_spread_l1, sell_spread_l1,  -- tightest level spreads
  skew_factor, fill_rate_1m, pnl_1h
)
```

The `rl_features` table is populated every 10 seconds from day 1, building a rich dataset for the Phase 2 RL agent training.
