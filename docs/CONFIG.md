# Configuration Reference

Complete reference for all configuration options in `bot.json` and `.env`.

---

## `bot.json` — Non-Secret Config (committed to git)

Contains all operational parameters. **Never put API keys here.**

Hot-reloadable parameters are marked ✅. Restart-required parameters are marked 🔄.

---

### Top-Level

| Key | Type | Default | Hot-reload | Description |
|-----|------|---------|------------|-------------|
| `dry_run` | bool | `true` | 🔄 | When `true`, calculates grids but never calls exchange write APIs |

> **Both `dry_run: false` AND `LIVE_MODE=true` must be set to place real orders.**

---

### `global_mid_weights`

Weights for the global reference price formula. Must sum to 1.0.

```json
"global_mid_weights": {
  "kucoin": 0.45,
  "gate":   0.45,
  "mexc":   0.05,
  "kraken": 0.05
}
```

| Key | Type | Constraint | Description |
|-----|------|-----------|-------------|
| `kucoin` | float | 0.0–1.0 | KuCoin weight |
| `gate` | float | 0.0–1.0 | Gate.io weight |
| `mexc` | float | 0.0–1.0 | MEXC weight |
| `kraken` | float | 0.0–1.0 | Kraken weight |

All four weights must sum to exactly 1.0 (validated by Pydantic on load).

Hot-reload: ✅ via `PUT /api/config`

---

### `volatility`

Controls how market volatility maps to aggressiveness.

```json
"volatility": {
  "window_minutes": 10,
  "low_threshold":  0.001,
  "high_threshold": 0.003,
  "power":          2.0
}
```

| Key | Type | Default | Hot-reload | Description |
|-----|------|---------|------------|-------------|
| `window_minutes` | int | `10` | ✅ | Rolling window for std-dev vol calculation |
| `low_threshold` | float | `0.001` | ✅ | vol ≤ this → aggressiveness = 1.0 |
| `high_threshold` | float | `0.003` | ✅ | vol ≥ this → aggressiveness = 0.0 |
| `power` | float | `2.0` | ✅ | Decay curve shape (2 = quadratic, 1 = linear) |

**Mapping formula:**
```
vol ≤ low_threshold  → agg = 1.0
vol ≥ high_threshold → agg = 0.0
otherwise            → agg = 1 − ((vol − low) / (high − low)) ^ power
```

---

### `exchanges` (array)

One entry per exchange. All 4 are required. Order does not matter.

```json
"exchanges": [
  {
    "exchange":       "kucoin",
    "symbol":         "ALKIMI/USDT",
    "quote_currency": "USDT",
    "enabled":        true,
    "spread":         { ... },
    "depth":          { ... },
    "safety":         { ... }
  }
]
```

#### Exchange-Level Fields

| Key | Type | Description |
|-----|------|-------------|
| `exchange` | string | One of: `kucoin`, `gate`, `mexc`, `kraken` |
| `symbol` | string | CCXT market symbol (e.g. `"ALKIMI/USDT"`, `"ALKIMI/USD"` for Kraken) |
| `quote_currency` | string | `"USDT"` for KuCoin/Gate/MEXC; `"USD"` for Kraken |
| `enabled` | bool | Set to `false` to disable a single exchange without removing its config |

---

#### `spread` — Spread Configuration

Controls where orders are placed relative to the mid-price.

```json
"spread": {
  "buy_min_pct":    -0.1,
  "buy_max_pct":    -5.0,
  "sell_min_pct":    0.3,
  "sell_max_pct":    7.0,
  "curve_strength":  4.0
}
```

| Key | Type | Default | Hot-reload | Description |
|-----|------|---------|------------|-------------|
| `buy_min_pct` | float | `-0.1` | ✅ | Tightest buy spread (% below mid). Must be negative. |
| `buy_max_pct` | float | `-5.0` | ✅ | Widest buy spread (% below mid). Must be more negative than min. |
| `sell_min_pct` | float | `0.3` | ✅ | Tightest sell spread (% above mid). Must be positive. |
| `sell_max_pct` | float | `7.0` | ✅ | Widest sell spread (% above mid). Must be > min. |
| `curve_strength` | float | `4.0` | ✅ | Power curve shape. Higher = more clustering at extremes. |

**Effect of `curve_strength`:**
- `1.0` = linear spacing (orders evenly distributed across spread range)
- `4.0` = aggressive clustering (default — orders heavily weighted toward one end based on aggressiveness)
- `8.0` = extreme clustering (all orders near tightest or widest depending on agg)

---

#### `depth` — Depth Configuration

Controls how much capital is allocated to each order level.

```json
"depth": {
  "levels":           15,
  "total_budget_usd": 1000.0
}
```

| Key | Type | Default | Hot-reload | Description |
|-----|------|---------|------------|-------------|
| `levels` | int | `15` | ✅ | Number of buy levels + number of sell levels |
| `total_budget_usd` | float | `1000.0` | ✅ | Total USD value across all orders (buy + sell combined) |

**Budget split:** The total budget is divided approximately 50/50 between buy and sell sides, then adjusted by `skew_factor` based on inventory drift.

**Per-level amounts:** At `n=15` levels and $1000 total:
- Aggressive (`agg=1`, equal distribution): ~$33 per level per side
- Passive (`agg=0`, geometric decay): ~$116 level 1, ~$93 level 2, down to ~$11 level 15

---

#### `safety` — Safety Configuration

Thresholds for the Q-Switch emergency stop.

```json
"safety": {
  "min_balance_usd":           50.0,
  "min_balance_token":        100.0,
  "max_daily_loss_pct":        10.0,
  "max_drawdown_pct":          15.0,
  "max_requests_per_second":    8.0
}
```

| Key | Type | Default | Hot-reload | Description |
|-----|------|---------|------------|-------------|
| `min_balance_usd` | float | `50.0` | ✅ | Q-Switch triggers if USD balance drops below this |
| `min_balance_token` | float | `100.0` | ✅ | Q-Switch triggers if ALKIMI balance drops below this |
| `max_daily_loss_pct` | float | `10.0` | ✅ | Circuit Breaker: halt if daily P&L loss exceeds this % |
| `max_drawdown_pct` | float | `15.0` | ✅ | Circuit Breaker: halt if drawdown from peak exceeds this % |
| `max_requests_per_second` | float | `8.0` | 🔄 | Rate limiter: max API calls per second to this exchange |

---

### `initial_balances`

The reference balances used by `InventoryTracker` to compute skew.

```json
"initial_balances": {
  "kucoin":  { "usd": 500.0, "token": 5000.0 },
  "gate":    { "usd": 500.0, "token": 5000.0 },
  "mexc":    { "usd": 250.0, "token": 2500.0 },
  "kraken":  { "usd": 250.0, "token": 2500.0 }
}
```

| Key | Type | Description |
|-----|------|-------------|
| `usd` | float | Initial USD/USDT balance on this exchange |
| `token` | float | Initial ALKIMI token balance on this exchange |

These values define "neutral" — when actual balances match initial, `skew_factor = 1.0`. Update these after significant capital additions or withdrawals.

Hot-reload: ✅ (takes effect on next balance fetch, ~10s)

---

## `.env` — Secrets (never committed)

### Exchange API Keys

| Variable | Required | Description |
|----------|----------|-------------|
| `KUCOIN_API_KEY` | ✅ | KuCoin API key |
| `KUCOIN_API_SECRET` | ✅ | KuCoin API secret |
| `KUCOIN_PASSPHRASE` | ✅ | KuCoin API passphrase (unique to KuCoin) |
| `GATE_API_KEY` | ✅ | Gate.io API key |
| `GATE_API_SECRET` | ✅ | Gate.io API secret |
| `MEXC_API_KEY` | ✅ | MEXC API key |
| `MEXC_API_SECRET` | ✅ | MEXC API secret |
| `KRAKEN_API_KEY` | ✅ | Kraken API key |
| `KRAKEN_API_SECRET` | ✅ | Kraken API secret (the "private key", not the secret key) |

### Runtime Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `LIVE_MODE` | `false` | Set to `true` to enable real order placement. **Both this AND `dry_run: false` must be set.** |
| `PORT` | `8000` | Port for the FastAPI server |
| `DB_PATH` | `data/mm_bot.db` | Path to SQLite database file |
| `BOT_CONFIG_PATH` | `bot.json` | Path to bot.json config file |
| `LOG_LEVEL` | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `ALERT_WEBHOOK_URL` | *(empty)* | Discord/Slack webhook URL for Q-Switch alerts. Leave blank to disable. |

### `.env.example` Template

```bash
# Exchange API Keys
KUCOIN_API_KEY=your_kucoin_api_key
KUCOIN_API_SECRET=your_kucoin_api_secret
KUCOIN_PASSPHRASE=your_kucoin_passphrase

GATE_API_KEY=your_gate_api_key
GATE_API_SECRET=your_gate_api_secret

MEXC_API_KEY=your_mexc_api_key
MEXC_API_SECRET=your_mexc_api_secret

KRAKEN_API_KEY=your_kraken_api_key
KRAKEN_API_SECRET=your_kraken_api_secret

# Runtime
LIVE_MODE=false
PORT=8000
DB_PATH=data/mm_bot.db
LOG_LEVEL=INFO

# Optional: Discord/Slack webhook for Q-Switch alerts
ALERT_WEBHOOK_URL=
```

---

## Configuration Validation

When the bot starts (or when `PUT /api/config` is called), all config is validated by Pydantic:

- **GlobalMidWeights**: weights must sum to 1.0 (within floating-point tolerance)
- **SpreadConfig**: `buy_max_pct < buy_min_pct < 0 < sell_min_pct < sell_max_pct`
- **DepthConfig**: `levels` ∈ [1, 50]; `total_budget_usd` > 0
- **VolatilityConfig**: `low_threshold < high_threshold`
- **SafetyConfig**: all thresholds > 0; `max_daily_loss_pct` ∈ (0, 100]

If validation fails, the bot refuses to start (or the PUT endpoint returns a 422 error).

---

## Hot-Reload Workflow

To update spread/depth/volatility parameters without restarting:

```bash
# 1. Fetch current config
curl http://localhost:8000/api/config

# 2. Modify the JSON (e.g. tighten sell spreads)
# 3. PUT the updated config
curl -X PUT http://localhost:8000/api/config \
  -H "Content-Type: application/json" \
  -d '{ "dry_run": true, "global_mid_weights": {...}, "volatility": {...}, ... }'
```

The server:
1. Validates the new config via Pydantic
2. Writes it to `bot.json`
3. Fires an `asyncio.Event` to the Orchestrator
4. Each `ExchangeBot` picks up new spread/depth params on the next tick
5. Broadcasts a `CONFIG_RELOADED` WebSocket event to all connected clients

**What requires a restart:**
- `LIVE_MODE` (environment variable)
- `dry_run` toggle (safety guard — requires deliberate restart)
- `max_requests_per_second` (rate limiter is initialised at startup)
- Any `.env` change (credentials, ports)

---

## Exchange-Specific Notes

### KuCoin

- Requires `KUCOIN_PASSPHRASE` in addition to key/secret — unique to KuCoin
- CCXT ID: `kucoin`
- Symbol format: `ALKIMI/USDT`
- Rate limit: 8 req/s recommended (KuCoin allows 30/s but burst protection helps)

### Gate.io

- CCXT ID: `gate`
- Symbol format: `ALKIMI/USDT`
- Note: Gate.io API uses `gateio` in some CCXT versions; factory handles this

### MEXC

- CCXT ID: `mexc`
- Symbol format: `ALKIMI/USDT`
- Lower liquidity → lower budget default ($500 vs $1000)

### Kraken

- Symbol format: `ALKIMI/USD` (USD, not USDT)
- `quote_currency: "USD"` — InventoryTracker normalises USD ≈ USDT at par for inventory calculations
- CCXT ID: `kraken`
- Kraken has stricter rate limits; 4 req/s recommended
