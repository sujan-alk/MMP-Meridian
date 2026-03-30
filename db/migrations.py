"""
SQLite schema migrations.
Runs on startup via database.py.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id              TEXT PRIMARY KEY,
    exchange        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,     -- 'buy' | 'sell'
    price           REAL NOT NULL,
    amount          REAL NOT NULL,     -- token amount
    amount_usd      REAL NOT NULL,     -- USD equivalent at placement
    status          TEXT NOT NULL,     -- 'open' | 'filled' | 'canceled' | 'partial'
    placed_at       REAL NOT NULL,     -- Unix timestamp (seconds)
    updated_at      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_exchange ON orders(exchange);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_placed_at ON orders(placed_at);

CREATE TABLE IF NOT EXISTS fills (
    id              TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL,
    exchange        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    filled_price    REAL NOT NULL,
    filled_amount   REAL NOT NULL,
    fee             REAL NOT NULL,
    fee_currency    TEXT NOT NULL,
    filled_at       REAL NOT NULL,     -- Unix timestamp (seconds)
    pnl_usd         REAL              -- Realized P&L if calculable (nullable)
);

CREATE INDEX IF NOT EXISTS idx_fills_exchange ON fills(exchange);
CREATE INDEX IF NOT EXISTS idx_fills_filled_at ON fills(filled_at);

CREATE TABLE IF NOT EXISTS inventory_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange            TEXT NOT NULL,
    usd                 REAL NOT NULL,
    token               REAL NOT NULL,
    global_mid          REAL NOT NULL,
    volatility          REAL NOT NULL,
    aggressiveness      REAL NOT NULL,
    skew_factor         REAL NOT NULL,
    snapshot_at         REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_exchange ON inventory_snapshots(exchange);
CREATE INDEX IF NOT EXISTS idx_snapshots_at ON inventory_snapshots(snapshot_at);

CREATE TABLE IF NOT EXISTS rl_features (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL NOT NULL,
    vol_simple      REAL,
    vol_zz          REAL,
    zz_regime       TEXT,
    aggressiveness  REAL,
    global_mid      REAL,
    buy_spread_l1   REAL,    -- tightest buy spread %
    sell_spread_l1  REAL,    -- tightest sell spread %
    skew_factor     REAL,
    fill_rate_1m    REAL,    -- fills per minute (rolling)
    pnl_1h          REAL     -- rolling 1-hour realized P&L
);

CREATE INDEX IF NOT EXISTS idx_rl_features_ts ON rl_features(timestamp);
"""
