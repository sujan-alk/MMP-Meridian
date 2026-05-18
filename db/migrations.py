"""
PostgreSQL schema migrations.
Runs on startup via database.py.
Each statement is executed individually (asyncpg does not support multi-statement execution).

All tables live in a named schema to keep them isolated from other tables in the
same database (e.g. shared Supabase project). Prod uses 'mm_bot'; tests use
'mm_bot_test' so prod and test can share a single Supabase project safely.
Override with the DB_SCHEMA env var.
"""

import os

SCHEMA_NAME = os.environ.get("DB_SCHEMA", "mm_bot")

SCHEMA_STATEMENTS: list[str] = [
    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME}",

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.orders (
        id              TEXT PRIMARY KEY,
        exchange        TEXT NOT NULL,
        symbol          TEXT NOT NULL,
        side            TEXT NOT NULL,
        price           DOUBLE PRECISION NOT NULL,
        amount          DOUBLE PRECISION NOT NULL,
        amount_usd      DOUBLE PRECISION NOT NULL,
        status          TEXT NOT NULL,
        placed_at       DOUBLE PRECISION NOT NULL,
        updated_at      DOUBLE PRECISION NOT NULL
    )
    """,
    f"CREATE INDEX IF NOT EXISTS idx_orders_exchange ON {SCHEMA_NAME}.orders(exchange)",
    f"CREATE INDEX IF NOT EXISTS idx_orders_status ON {SCHEMA_NAME}.orders(status)",
    f"CREATE INDEX IF NOT EXISTS idx_orders_placed_at ON {SCHEMA_NAME}.orders(placed_at)",

    # ------------------------------------------------------------------
    # Fills
    # ------------------------------------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.fills (
        id              TEXT PRIMARY KEY,
        order_id        TEXT NOT NULL,
        exchange        TEXT NOT NULL,
        symbol          TEXT NOT NULL,
        side            TEXT NOT NULL,
        filled_price    DOUBLE PRECISION NOT NULL,
        filled_amount   DOUBLE PRECISION NOT NULL,
        fee             DOUBLE PRECISION NOT NULL,
        fee_currency    TEXT NOT NULL,
        filled_at       DOUBLE PRECISION NOT NULL,
        pnl_usd         DOUBLE PRECISION
    )
    """,
    f"CREATE INDEX IF NOT EXISTS idx_fills_exchange ON {SCHEMA_NAME}.fills(exchange)",
    f"CREATE INDEX IF NOT EXISTS idx_fills_filled_at ON {SCHEMA_NAME}.fills(filled_at)",

    # ------------------------------------------------------------------
    # Inventory snapshots
    # ------------------------------------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.inventory_snapshots (
        id                  SERIAL PRIMARY KEY,
        exchange            TEXT NOT NULL,
        usd                 DOUBLE PRECISION NOT NULL,
        token               DOUBLE PRECISION NOT NULL,
        global_mid          DOUBLE PRECISION NOT NULL,
        volatility          DOUBLE PRECISION NOT NULL,
        aggressiveness      DOUBLE PRECISION NOT NULL,
        skew_factor         DOUBLE PRECISION NOT NULL,
        snapshot_at         DOUBLE PRECISION NOT NULL
    )
    """,
    f"CREATE INDEX IF NOT EXISTS idx_snapshots_exchange ON {SCHEMA_NAME}.inventory_snapshots(exchange)",
    f"CREATE INDEX IF NOT EXISTS idx_snapshots_at ON {SCHEMA_NAME}.inventory_snapshots(snapshot_at)",

    # ------------------------------------------------------------------
    # RL features
    # ------------------------------------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.rl_features (
        id              SERIAL PRIMARY KEY,
        timestamp       DOUBLE PRECISION NOT NULL,
        vol_simple      DOUBLE PRECISION,
        vol_zz          DOUBLE PRECISION,
        zz_regime       TEXT,
        hmm_regime      TEXT,
        hmm_confidence  DOUBLE PRECISION,
        aggressiveness  DOUBLE PRECISION,
        global_mid      DOUBLE PRECISION,
        buy_spread_l1   DOUBLE PRECISION,
        sell_spread_l1  DOUBLE PRECISION,
        skew_factor     DOUBLE PRECISION,
        fill_rate_1m    DOUBLE PRECISION,
        pnl_1h          DOUBLE PRECISION
    )
    """,
    f"CREATE INDEX IF NOT EXISTS idx_rl_features_ts ON {SCHEMA_NAME}.rl_features(timestamp)",

    # Migration: add hmm columns to existing rl_features tables
    f"""
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = '{SCHEMA_NAME}' AND table_name = 'rl_features' AND column_name = 'hmm_regime'
        ) THEN
            ALTER TABLE {SCHEMA_NAME}.rl_features ADD COLUMN hmm_regime TEXT;
            ALTER TABLE {SCHEMA_NAME}.rl_features ADD COLUMN hmm_confidence DOUBLE PRECISION;
        END IF;
    END $$
    """,
]
