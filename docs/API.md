# API Reference

The bot exposes a FastAPI server (default port 8000) with REST endpoints and a WebSocket live feed.

Interactive docs available at `http://localhost:8000/docs` when running.

---

## Base URL

```
http://localhost:8000
```

On Railway: `https://<your-app>.railway.app`

---

## REST Endpoints

### `GET /health`

Liveness check. Used by Railway's healthcheck.

**Response 200:**
```json
{
  "status": "ok",
  "timestamp": 1743340800.123
}
```

---

### `GET /api/status`

Aggregated status across all 4 exchanges plus global metrics.

**Response 200:**
```json
{
  "global_mid": 0.0234,
  "volatility": 0.0012,
  "aggressiveness": 0.82,
  "zz_vol": null,
  "zz_regime": null,
  "contributing_exchanges": ["kucoin", "gate", "mexc", "kraken"],
  "exchanges": {
    "kucoin": {
      "exchange": "kucoin",
      "symbol": "ALKIMI/USDT",
      "enabled": true,
      "running": true,
      "paused": false,
      "q_switch_active": false,
      "circuit_breaker_tripped": false,
      "last_tick_at": 1743340799.5,
      "tick_count": 1847,
      "open_orders_count": 30,
      "balance_usd": 487.32,
      "balance_token": 4832.0,
      "skew_factor": 1.07,
      "aggressiveness": 0.82,
      "spread_l1_buy_pct": -0.11,
      "spread_l1_sell_pct": 0.31,
      "error": null
    }
  },
  "timestamp": 1743340800.0
}
```

---

### `GET /api/exchanges/{exchange}`

Status for a single exchange.

**Path Parameters:**
| Parameter | Values |
|-----------|--------|
| `exchange` | `kucoin`, `gate`, `mexc`, `kraken` |

**Response 200:** Same schema as a single entry in `/api/status → exchanges`.

**Response 404:** `{"detail": "Exchange 'foo' not found"}`

---

### `GET /api/orders`

All open orders across all exchanges.

**Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `exchange` | string | *(all)* | Filter by exchange |
| `side` | string | *(all)* | `buy` or `sell` |

**Response 200:**
```json
{
  "orders": [
    {
      "id": "order-uuid-123",
      "exchange": "kucoin",
      "symbol": "ALKIMI/USDT",
      "side": "buy",
      "price": 0.02316,
      "amount": 1423.5,
      "amount_usd": 32.95,
      "status": "open",
      "placed_at": 1743340750.0,
      "updated_at": 1743340750.0
    }
  ],
  "total": 120,
  "timestamp": 1743340800.0
}
```

---

### `GET /api/fills`

Fill history, paginated.

**Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `exchange` | string | *(all)* | Filter by exchange |
| `side` | string | *(all)* | `buy` or `sell` |
| `limit` | int | `100` | Max results |
| `offset` | int | `0` | Pagination offset |

**Response 200:**
```json
{
  "fills": [
    {
      "id": "fill-uuid-456",
      "order_id": "order-uuid-123",
      "exchange": "kucoin",
      "symbol": "ALKIMI/USDT",
      "side": "buy",
      "filled_price": 0.02316,
      "filled_amount": 1423.5,
      "fee": 0.03295,
      "fee_currency": "USDT",
      "filled_at": 1743340760.0,
      "pnl_usd": null
    }
  ],
  "total": 847,
  "limit": 100,
  "offset": 0
}
```

---

### `GET /api/balances`

Current balances per exchange plus inventory drift.

**Response 200:**
```json
{
  "balances": {
    "kucoin": {
      "usd": 487.32,
      "token": 4832.0,
      "initial_usd": 500.0,
      "initial_token": 5000.0,
      "token_drift_pct": -3.36,
      "skew_factor": 1.07,
      "should_rebalance": false
    },
    "gate": { ... },
    "mexc": { ... },
    "kraken": { ... }
  },
  "total_usd": 2012.44,
  "total_token": 19432.0,
  "timestamp": 1743340800.0
}
```

---

### `GET /api/metrics`

Key performance and model metrics.

**Response 200:**
```json
{
  "volatility": 0.0012,
  "aggressiveness": 0.82,
  "global_mid": 0.0234,
  "zz_vol": null,
  "zz_regime": null,
  "fill_rate_1m": {
    "kucoin": 2.3,
    "gate": 1.8,
    "mexc": 0.4,
    "kraken": 0.1
  },
  "pnl_1h_usd": {
    "kucoin": 1.23,
    "gate": 0.87,
    "mexc": -0.12,
    "kraken": 0.05
  },
  "total_fills_today": 127,
  "uptime_seconds": 84200,
  "timestamp": 1743340800.0
}
```

---

### `GET /api/config`

Returns the current `bot.json` content.

**Response 200:** Full `BotConfig` as JSON — same schema as `bot.json`.

---

### `PUT /api/config`

Hot-reload the full bot configuration. Validates, saves to `bot.json`, and applies immediately.

**Request Body:** Full `BotConfig` JSON (same schema as `bot.json`).

```json
{
  "dry_run": true,
  "global_mid_weights": {
    "kucoin": 0.45,
    "gate":   0.45,
    "mexc":   0.05,
    "kraken": 0.05
  },
  "volatility": {
    "window_minutes": 10,
    "low_threshold":  0.001,
    "high_threshold": 0.003,
    "power":          2.0
  },
  "exchanges": [ ... ],
  "initial_balances": { ... }
}
```

**Response 200:**
```json
{
  "status": "reloaded",
  "message": "Config validated, saved, and applied",
  "timestamp": 1743340800.0
}
```

**Response 422:** Pydantic validation error with details of which field failed.

> **Note:** `dry_run` changes require a restart to take effect. The endpoint will accept the config but log a warning if `dry_run` changes value.

---

### `PUT /api/config/exchanges/{exchange}`

Update a single exchange's spread/depth/safety config without touching other exchanges.

**Path Parameters:** `exchange` = one of `kucoin`, `gate`, `mexc`, `kraken`

**Request Body:** `ExchangeBotConfig` for just that exchange.

**Response 200:** Same as `PUT /api/config`.

---

### `POST /api/control/pause`

Pause all bots. Orders remain on the book but no new ticks are processed.

**Response 200:**
```json
{ "status": "paused", "exchanges": ["kucoin", "gate", "mexc", "kraken"] }
```

---

### `POST /api/control/resume`

Resume all paused bots. Only works if Q-Switch has not been triggered (use `/emergency_stop` reset path for that).

**Response 200:**
```json
{ "status": "resumed", "exchanges": ["kucoin", "gate", "mexc", "kraken"] }
```

---

### `POST /api/control/emergency_stop`

Immediately cancels all open orders on all exchanges and halts all bots.

This triggers the Q-Switch manually. After an emergency stop, bots require an explicit `/resume` call and a Q-Switch reset via the API.

**Response 200:**
```json
{
  "status": "emergency_stop",
  "message": "All orders cancelled. Manual API resume required.",
  "timestamp": 1743340800.0
}
```

---

### `POST /api/control/reset_circuit_breaker`

Reset the circuit breaker on a specific exchange (or all) after manual review.

**Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `exchange` | string | *(all)* | Target exchange, or omit for all |

**Response 200:**
```json
{ "status": "reset", "exchange": "kucoin" }
```

---

## WebSocket

### `WS /ws`

Real-time event stream. Connect with any WebSocket client.

```
ws://localhost:8000/ws
```

**Behaviour:**
- Server sends events as they occur
- Every 30 seconds of inactivity, server sends a `heartbeat` ping
- Client can send any message to reset the inactivity timer
- Connection drops silently if client queue falls behind (slow consumers are dropped)

### Event Schema

All events follow this envelope:

```json
{
  "event": "event_type_name",
  "data": { ... },
  "timestamp": 1743340800.0
}
```

---

### Event Types

#### `tick_update`

Emitted every ~1 second per exchange after each successful tick.

```json
{
  "event": "tick_update",
  "data": {
    "exchange": "kucoin",
    "global_mid": 0.0234,
    "volatility": 0.0012,
    "aggressiveness": 0.82,
    "skew_factor": 1.07,
    "open_orders": 30,
    "orders_placed_this_tick": 0,
    "orders_cancelled_this_tick": 0,
    "balance_usd": 487.32,
    "balance_token": 4832.0
  },
  "timestamp": 1743340800.0
}
```

---

#### `order_placed`

Emitted when a new order is placed on an exchange.

```json
{
  "event": "order_placed",
  "data": {
    "exchange": "kucoin",
    "order_id": "order-uuid-123",
    "side": "buy",
    "price": 0.02316,
    "amount": 1423.5,
    "amount_usd": 32.95
  },
  "timestamp": 1743340800.0
}
```

---

#### `order_filled`

Emitted when a fill is detected during `poll_fills()`.

```json
{
  "event": "order_filled",
  "data": {
    "exchange": "kucoin",
    "order_id": "order-uuid-123",
    "side": "buy",
    "filled_price": 0.02316,
    "filled_amount": 1423.5,
    "fee": 0.032,
    "pnl_usd": null
  },
  "timestamp": 1743340800.0
}
```

---

#### `order_canceled`

Emitted when an order is cancelled by the diff-and-repost logic.

```json
{
  "event": "order_canceled",
  "data": {
    "exchange": "kucoin",
    "order_id": "order-uuid-old",
    "reason": "price_stale"
  },
  "timestamp": 1743340800.0
}
```

---

#### `inventory_update`

Emitted every 30 seconds when an inventory snapshot is written to the DB.

```json
{
  "event": "inventory_update",
  "data": {
    "exchange": "kucoin",
    "usd": 487.32,
    "token": 4832.0,
    "skew_factor": 1.07,
    "token_drift_pct": -3.36
  },
  "timestamp": 1743340800.0
}
```

---

#### `aggressiveness_change`

Emitted when aggressiveness crosses a 10% threshold (to avoid flooding).

```json
{
  "event": "aggressiveness_change",
  "data": {
    "previous": 0.92,
    "current": 0.82,
    "volatility": 0.0012,
    "direction": "decreasing"
  },
  "timestamp": 1743340800.0
}
```

---

#### `emergency_stop`

Emitted when any Q-Switch fires, heartbeat fails, or manual emergency stop is called.

```json
{
  "event": "emergency_stop",
  "data": {
    "exchange": "kucoin",
    "reason": "balance_breach",
    "trigger": "q_switch",
    "balance_usd": 45.0,
    "min_balance_usd": 50.0
  },
  "timestamp": 1743340800.0
}
```

---

#### `heartbeat`

Sent by server every 30 seconds of inactivity to keep the connection alive.

```json
{
  "event": "heartbeat",
  "data": { "server_time": 1743340800.0 },
  "timestamp": 1743340800.0
}
```

---

#### `config_reloaded`

Emitted after a successful `PUT /api/config`.

```json
{
  "event": "config_reloaded",
  "data": {
    "changed_fields": ["volatility.window_minutes", "exchanges.kucoin.spread.sell_max_pct"]
  },
  "timestamp": 1743340800.0
}
```

---

#### `bot_started` / `bot_stopped`

Emitted when an individual exchange bot starts or stops.

```json
{
  "event": "bot_started",
  "data": { "exchange": "kucoin" },
  "timestamp": 1743340800.0
}
```

---

## WebSocket Client Example

### Python (asyncio)

```python
import asyncio
import json
import websockets

async def monitor():
    async with websockets.connect("ws://localhost:8000/ws") as ws:
        async for message in ws:
            event = json.loads(message)
            print(f"[{event['event']}] {event['data']}")

asyncio.run(monitor())
```

### JavaScript (browser)

```javascript
const ws = new WebSocket("ws://localhost:8000/ws");

ws.onmessage = (event) => {
  const { event: type, data, timestamp } = JSON.parse(event.data);
  console.log(`[${type}]`, data);
};

ws.onclose = () => {
  console.log("Connection closed — reconnecting...");
  setTimeout(() => connect(), 2000);
};
```

---

## Error Responses

All REST endpoints return standard FastAPI error responses:

| Status | Meaning |
|--------|---------|
| `200` | Success |
| `404` | Exchange not found |
| `422` | Validation error (bad config, invalid field) |
| `500` | Internal server error (check logs) |

Error body:
```json
{
  "detail": "Human-readable description of the error"
}
```

For `422` validation errors, `detail` is a list of Pydantic validation error objects with `loc`, `msg`, and `type` fields.
