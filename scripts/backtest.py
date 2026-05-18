"""
Historical backtest — replays the bot's market making logic against
historical OHLCV candles using a synthetic order book model.

Pipeline per candle:
1. Push close price into VolatilityEngine
2. Compute simple vol → aggressiveness
3. Generate buy/sell grid via SpreadEngine + DepthEngine
4. Synthesise an order book around the candle's high/low range
5. Run FillSimulator against the synthetic book
6. Track inventory drift, fees, P&L, fill counts

Synthetic book model (per candle):
  mid       = close
  spread    = max((high - low) / close, MIN_SPREAD_PCT)
  best_bid  = mid * (1 - spread/2)
  best_ask  = mid * (1 + spread/2)
  Ladder of N levels, geometrically tapered prices, depth proportional to volume

Run:
  python3 scripts/backtest.py --exchange kucoin --days 30 --capital 1000
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
import time
from dataclasses import dataclass

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ccxt.async_support as ccxt

from config.schema import (
    DepthConfig,
    ExchangeBotConfig,
    SafetyConfig,
    SpreadConfig,
    VolatilityConfig,
)
from core.fill_simulator import FillSimulator
from core.inventory_tracker import InventoryTracker
from exchange.base import Balance, Candle, Order
from exchange.ws_connector import OrderBook
from quant.aggressiveness import AggressivenessModel
from quant.depth_engine import DepthEngine
from quant.spread_engine import SpreadEngine
from quant.volatility import VolatilityEngine


# ---------------------------------------------------------------------------
# Synthetic book model
# ---------------------------------------------------------------------------

MIN_SPREAD_PCT = 0.001    # 10 bps minimum spread
MAX_SPREAD_PCT = 0.02     # 200 bps cap
BOOK_LEVELS = 20          # Levels per side
BOOK_DEPTH_DECAY = 0.85   # Each level holds 85% of the previous level's qty


def synthesise_book(candle: Candle, exchange: str) -> OrderBook:
    """Generate a synthetic order book from a candle."""
    mid = candle.close
    if mid <= 0:
        return OrderBook(exchange=exchange, bids=[], asks=[], timestamp=candle.timestamp)

    # Spread proxy from candle range
    raw_spread = (candle.high - candle.low) / mid if mid > 0 else MIN_SPREAD_PCT
    spread_pct = max(MIN_SPREAD_PCT, min(MAX_SPREAD_PCT, raw_spread))

    half_spread = mid * spread_pct / 2.0
    best_bid = mid - half_spread
    best_ask = mid + half_spread

    # Total liquidity to put in the book ≈ candle volume in tokens
    total_qty = max(candle.volume, 1000.0)
    level_qty = total_qty / BOOK_LEVELS

    # Geometric price ladder + decaying depth
    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    qty = level_qty
    step = mid * 0.0005  # 5 bps between levels
    for i in range(BOOK_LEVELS):
        bids.append((best_bid - i * step, qty))
        asks.append((best_ask + i * step, qty))
        qty *= BOOK_DEPTH_DECAY

    return OrderBook(
        exchange=exchange,
        bids=bids,
        asks=asks,
        timestamp=candle.timestamp,
    )


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

async def fetch_candles(exchange_name: str, symbol: str, days: int) -> list[Candle]:
    """Fetch historical 1m OHLCV candles via CCXT."""
    print(f"Fetching {days} days of {symbol} candles from {exchange_name}...")
    exchange_class = getattr(ccxt, exchange_name)
    ex = exchange_class({"enableRateLimit": True})
    try:
        await ex.load_markets()

        timeframe = "1m"
        limit = 1000  # Most exchanges cap at 500-1500
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - days * 24 * 60 * 60 * 1000

        all_rows: list[list] = []
        cursor = start_ms
        while cursor < end_ms:
            rows = await ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=limit)
            if not rows:
                break
            all_rows.extend(rows)
            last_ts = rows[-1][0]
            if last_ts <= cursor:
                break
            cursor = last_ts + 60_000  # advance one minute past last
            await asyncio.sleep(ex.rateLimit / 1000.0)

        candles = [
            Candle(
                timestamp=row[0] / 1000.0,
                open=row[1],
                high=row[2],
                low=row[3],
                close=row[4],
                volume=row[5],
            )
            for row in all_rows
        ]
        print(f"  Fetched {len(candles)} candles "
              f"({candles[0].timestamp if candles else 0:.0f} → {candles[-1].timestamp if candles else 0:.0f})")
        return candles
    finally:
        await ex.close()


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

@dataclass
class BacktestStats:
    total_fills: int = 0
    buy_fills: int = 0
    sell_fills: int = 0
    total_buy_qty: float = 0.0
    total_sell_qty: float = 0.0
    total_buy_usd: float = 0.0
    total_sell_usd: float = 0.0
    total_fees_usd: float = 0.0
    realised_pnl_usd: float = 0.0


def make_grid_orders(
    exchange: str,
    buy_prices: list[float],
    buy_amounts: list[float],
    sell_prices: list[float],
    sell_amounts: list[float],
    tick_id: int,
) -> list[Order]:
    """Convert grid prices/amounts into Order objects for FillSimulator."""
    orders: list[Order] = []
    for i, (p, a) in enumerate(zip(buy_prices, buy_amounts)):
        if p <= 0 or a <= 0:
            continue
        orders.append(Order(
            id=f"bt_{tick_id}_b{i}",
            exchange=exchange,
            symbol="ALKIMI/USDT",
            side="buy",
            price=p,
            amount=a,
            amount_usd=p * a,
            status="open",
            timestamp=float(tick_id),
        ))
    for i, (p, a) in enumerate(zip(sell_prices, sell_amounts)):
        if p <= 0 or a <= 0:
            continue
        orders.append(Order(
            id=f"bt_{tick_id}_s{i}",
            exchange=exchange,
            symbol="ALKIMI/USDT",
            side="sell",
            price=p,
            amount=a,
            amount_usd=p * a,
            status="open",
            timestamp=float(tick_id),
        ))
    return orders


def run_backtest(
    candles: list[Candle],
    exchange: str,
    initial_usd: float,
    initial_token: float,
    output_csv: str | None,
) -> BacktestStats:
    """Run the backtest loop and return aggregate stats."""

    # --- Configs (mirror production defaults) ---
    vol_cfg = VolatilityConfig(
        window_minutes=10,
        low_threshold=0.001,
        high_threshold=0.003,
        power=2.0,
    )
    spread_cfg = SpreadConfig(
        buy_min_pct=-5.0,
        buy_max_pct=-0.1,
        sell_min_pct=0.3,
        sell_max_pct=7.0,
        curve_strength=4.0,
    )
    depth_cfg = DepthConfig(
        levels=15,
        total_budget_usd=initial_usd,
        curve_strength=4.0,
        min_order_usd=5.0,
    )
    safety_cfg = SafetyConfig(
        min_balance_usd=10.0,
        min_balance_token=10.0,
        max_requests_per_second=15,
        heartbeat_interval_s=5.0,
        max_missed_heartbeats=3,
        max_daily_loss_pct=10.0,
        max_drawdown_pct=15.0,
    )
    ex_cfg = ExchangeBotConfig(
        exchange=exchange,
        symbol="ALKIMI/USDT",
        spread=spread_cfg,
        depth=depth_cfg,
        safety=safety_cfg,
    )

    vol_engine = VolatilityEngine(vol_cfg)
    agg_model = AggressivenessModel(vol_cfg)
    spread_engine = SpreadEngine(spread_cfg)
    depth_engine = DepthEngine(depth_cfg)
    inventory = InventoryTracker(ex_cfg)
    inventory.record_initial(Balance(usd=initial_usd, token=initial_token))

    simulator = FillSimulator(maker_fee_bps=10.0)
    stats = BacktestStats()

    # Running balances (mark-to-market each step)
    usd_balance = initial_usd
    token_balance = initial_token

    csv_rows: list[dict] = []

    print(f"Running backtest on {len(candles)} candles...")
    for tick_id, candle in enumerate(candles):
        # 1. Update volatility
        vol_engine.update_price(candle.close)

        # Need warmup before trading
        if vol_engine.sample_count < vol_cfg.window_minutes:
            continue

        vol = vol_engine.rolling_vol()
        mid = candle.close

        # 2. Compute aggressiveness
        agg = agg_model.compute(vol)
        buy_agg, sell_agg = agg, agg  # No regime adjustment in backtest

        # 3. Inventory skew
        inventory.update(Balance(usd=usd_balance, token=token_balance))
        skew = inventory.skew_factor()

        # 4. Generate grid
        n_levels = depth_cfg.levels
        buy_spreads, sell_spreads = spread_engine.compute_levels_dual(
            buy_agg, sell_agg, n_levels=n_levels
        )
        buy_prices, sell_prices = spread_engine.prices_from_spreads(
            mid, buy_spreads, sell_spreads
        )
        buy_usd = depth_engine.compute_amounts(buy_agg, n_levels, skew, "buy")
        sell_usd = depth_engine.compute_amounts(sell_agg, n_levels, skew, "sell")
        buy_amounts = [
            depth_engine.usd_to_token_amount(u, p)
            for u, p in zip(buy_usd, buy_prices)
        ]
        sell_amounts = [
            depth_engine.usd_to_token_amount(u, p)
            for u, p in zip(sell_usd, sell_prices)
        ]

        # Cap orders by available balance
        buy_amounts = [
            min(a, max(0.0, usd_balance / max(p, 1e-9)))
            for a, p in zip(buy_amounts, buy_prices)
        ]
        sell_amounts = [min(a, max(0.0, token_balance)) for a in sell_amounts]

        # 5. Make orders + run fill simulator
        orders = make_grid_orders(
            exchange, buy_prices, buy_amounts, sell_prices, sell_amounts, tick_id
        )
        book = synthesise_book(candle, exchange)
        fills = simulator.simulate(orders, book, mid)

        # 6. Update balances + stats
        for f in fills:
            stats.total_fills += 1
            notional = f.price * f.amount
            stats.total_fees_usd += f.fee
            if f.side == "buy":
                stats.buy_fills += 1
                stats.total_buy_qty += f.amount
                stats.total_buy_usd += notional
                usd_balance -= notional + f.fee
                token_balance += f.amount
            else:
                stats.sell_fills += 1
                stats.total_sell_qty += f.amount
                stats.total_sell_usd += notional
                stats.realised_pnl_usd += f.pnl_usd
                usd_balance += notional - f.fee
                token_balance -= f.amount

        # CSV trace row (one per candle)
        if output_csv and tick_id % 5 == 0:  # downsample
            mtm = usd_balance + token_balance * mid
            csv_rows.append({
                "ts": int(candle.timestamp),
                "mid": round(mid, 8),
                "vol": round(vol, 8),
                "agg": round(agg, 4),
                "skew": round(skew, 4),
                "fills_so_far": stats.total_fills,
                "usd_balance": round(usd_balance, 4),
                "token_balance": round(token_balance, 2),
                "mtm_usd": round(mtm, 4),
            })

    # Final mark-to-market
    final_mid = candles[-1].close if candles else 0.0
    final_mtm = usd_balance + token_balance * final_mid
    initial_mtm = initial_usd + initial_token * (candles[0].close if candles else 0.0)

    # ---- Print summary ----
    print()
    print("=" * 70)
    print("  BACKTEST RESULTS")
    print("=" * 70)
    print(f"  Period:           {len(candles)} candles ({len(candles)/1440:.1f} days)")
    print(f"  Exchange:         {exchange}")
    print(f"  Initial capital:  ${initial_usd:,.2f} + {initial_token:,.0f} ALKIMI")
    print(f"  Initial MTM:      ${initial_mtm:,.2f}")
    print(f"  Final MTM:        ${final_mtm:,.2f}")
    print(f"  Net P&L:          ${final_mtm - initial_mtm:,.2f} "
          f"({(final_mtm - initial_mtm) / max(initial_mtm, 1e-9) * 100:+.2f}%)")
    print()
    print(f"  Total fills:      {stats.total_fills:,}")
    print(f"    Buys:           {stats.buy_fills:,}")
    print(f"    Sells:          {stats.sell_fills:,}")
    print(f"  Buy volume:       {stats.total_buy_qty:,.0f} ALKIMI "
          f"(${stats.total_buy_usd:,.2f})")
    print(f"  Sell volume:      {stats.total_sell_qty:,.0f} ALKIMI "
          f"(${stats.total_sell_usd:,.2f})")
    print(f"  Total fees paid:  ${stats.total_fees_usd:,.2f}")
    print(f"  Realised P&L:     ${stats.realised_pnl_usd:,.2f}  "
          f"(spread capture from sells)")
    print()
    print(f"  Final USD:        ${usd_balance:,.2f}")
    print(f"  Final ALKIMI:     {token_balance:,.0f}")
    print("=" * 70)

    if output_csv and csv_rows:
        with open(output_csv, "w", newline="") as fp:
            writer = csv.DictWriter(fp, fieldnames=list(csv_rows[0].keys()))
            writer.writeheader()
            writer.writerows(csv_rows)
        print(f"  Trace written to {output_csv} ({len(csv_rows)} rows)")

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(description="MM bot historical backtest")
    parser.add_argument("--exchange", default="kucoin",
                        help="CCXT exchange id (kucoin, gate, mexc, kraken)")
    parser.add_argument("--symbol", default="ALKIMI/USDT")
    parser.add_argument("--days", type=int, default=30, help="Lookback days")
    parser.add_argument("--capital", type=float, default=1000.0,
                        help="Starting USD capital")
    parser.add_argument("--tokens", type=float, default=10000.0,
                        help="Starting ALKIMI balance")
    parser.add_argument("--out", default="backtest_trace.csv",
                        help="CSV output path (set to '' to skip)")
    args = parser.parse_args()

    candles = await fetch_candles(args.exchange, args.symbol, args.days)
    if not candles:
        print("No candles fetched — aborting.")
        return

    run_backtest(
        candles=candles,
        exchange=args.exchange,
        initial_usd=args.capital,
        initial_token=args.tokens,
        output_csv=args.out or None,
    )


if __name__ == "__main__":
    asyncio.run(main())
