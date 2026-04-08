"""
Dry-run demo — exercises the full quant pipeline with simulated prices.

No exchange credentials required. Simulates:
1. Price feed → volatility engine
2. Volatility → aggressiveness
3. Aggressiveness → spread levels + depth amounts
4. Inventory drift → skew factor
5. Order grid generation

Run: python3 scripts/dry_run_demo.py
"""

from __future__ import annotations

import math
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.schema import SpreadConfig, DepthConfig, VolatilityConfig, ExchangeBotConfig
from exchange.base import Balance, Candle
from core.inventory_tracker import InventoryTracker
from quant.aggressiveness import AggressivenessModel
from quant.depth_engine import DepthEngine
from quant.spread_engine import SpreadEngine
from quant.volatility import VolatilityEngine


def fmt_price(p: float) -> str:
    return f"${p:.6f}"


def fmt_pct(p: float) -> str:
    return f"{p:+.3f}%"


def fmt_usd(u: float) -> str:
    return f"${u:.2f}"


def simulate_price_feed(base: float = 0.015, minutes: int = 10, volatility: float = 0.002) -> list[float]:
    """Generate a realistic price feed with controlled volatility."""
    import random
    random.seed(42)
    prices = [base]
    for i in range(minutes * 60):
        ret = random.gauss(0, volatility)
        prices.append(prices[-1] * (1 + ret))
    return prices


def simulate_candles(prices: list[float], period: int = 60) -> list[Candle]:
    """Convert a price series into OHLCV candles."""
    candles = []
    for i in range(0, len(prices) - period, period):
        chunk = prices[i:i + period]
        candles.append(Candle(
            timestamp=float(i),
            open=chunk[0],
            high=max(chunk),
            low=min(chunk),
            close=chunk[-1],
            volume=10000.0,
        ))
    return candles


def main():
    print("=" * 70)
    print("  ALKIMI MM Bot — Dry Run Demo (no exchange connection)")
    print("=" * 70)

    # --- Config ---
    vol_cfg = VolatilityConfig(window_minutes=10, low_threshold=0.001, high_threshold=0.003, power=2.0)
    spread_cfg = SpreadConfig(buy_min_pct=-5.0, buy_max_pct=-0.1, sell_min_pct=0.3, sell_max_pct=7.0, curve_strength=4.0)
    depth_cfg = DepthConfig(levels=5, total_budget_usd=1000.0, curve_strength=4.0, min_order_usd=5.0)
    ex_cfg = ExchangeBotConfig(exchange="kucoin", symbol="ALKIMI/USDT", spread=spread_cfg, depth=depth_cfg)

    vol_engine = VolatilityEngine(vol_cfg)
    agg_model = AggressivenessModel(vol_cfg)
    spread_engine = SpreadEngine(spread_cfg)
    depth_engine = DepthEngine(depth_cfg)
    inventory = InventoryTracker(ex_cfg)

    # --- Simulate price feed ---
    print("\n[1] Simulating 10-minute price feed (ALKIMI ≈ $0.015)...")
    prices = simulate_price_feed(base=0.015, minutes=10, volatility=0.002)
    for p in prices:
        vol_engine.update_price(p)

    # --- Simulate candles for Zhang-Zhang ---
    candles = simulate_candles(prices)
    vol_engine.update_candles(candles)

    global_mid = prices[-1]
    print(f"    Final mid-price: {fmt_price(global_mid)}")
    print(f"    Prices fed: {vol_engine.sample_count}")
    print(f"    Candles fed: {vol_engine.candle_count}")

    # --- Volatility ---
    print("\n[2] Volatility computation...")
    vol = vol_engine.rolling_vol()
    zz_vol, zz_regime = vol_engine.zhang_zhang_vol()
    print(f"    Simple rolling vol: {vol:.6f}")
    print(f"    Zhang-Zhang vol:   {zz_vol:.6f}  regime={zz_regime}")

    # --- Aggressiveness ---
    print("\n[3] Aggressiveness mapping...")
    agg = agg_model.compute(vol)
    buy_agg, sell_agg = agg_model.compute_with_regime(vol, zz_regime)
    print(f"    Base aggressiveness: {agg:.4f}")
    print(f"    Buy agg (regime-adjusted):  {buy_agg:.4f}")
    print(f"    Sell agg (regime-adjusted): {sell_agg:.4f}")

    # --- Inventory & Skew ---
    print("\n[4] Inventory tracking...")
    inventory.record_initial(Balance(usd=500.0, token=5000.0))
    # Simulate having sold some tokens
    inventory.update(Balance(usd=650.0, token=4200.0))
    skew = inventory.skew_factor()
    state = inventory.state()
    print(f"    Initial: {fmt_usd(state.initial_usd)} + {state.initial_token:.0f} ALKIMI")
    print(f"    Current: {fmt_usd(state.usd)} + {state.token:.0f} ALKIMI")
    print(f"    Token drift: {state.token_drift_pct:+.1f}%")
    print(f"    Skew factor: {skew:.4f}  (>1 = buy more, <1 = sell more)")

    # --- Spread Levels ---
    print("\n[5] Spread levels (5 per side)...")
    buy_spreads, sell_spreads = spread_engine.compute_levels_dual(buy_agg, sell_agg, n_levels=5)
    buy_prices, sell_prices = spread_engine.prices_from_spreads(global_mid, buy_spreads, sell_spreads)

    print(f"\n    {'Level':<6} {'Buy Spread':<12} {'Buy Price':<14} {'Sell Spread':<12} {'Sell Price':<14}")
    print(f"    {'─' * 58}")
    for i in range(5):
        print(f"    {i:<6} {fmt_pct(buy_spreads[i]):<12} {fmt_price(buy_prices[i]):<14} {fmt_pct(sell_spreads[i]):<12} {fmt_price(sell_prices[i]):<14}")

    # --- Depth Amounts ---
    print(f"\n[6] Depth distribution (skew={skew:.2f})...")
    buy_usd = depth_engine.compute_amounts(buy_agg, 5, skew, "buy")
    sell_usd = depth_engine.compute_amounts(sell_agg, 5, skew, "sell")
    buy_tokens = [depth_engine.usd_to_token_amount(u, p) for u, p in zip(buy_usd, buy_prices)]
    sell_tokens = [depth_engine.usd_to_token_amount(u, p) for u, p in zip(sell_usd, sell_prices)]

    print(f"\n    {'Level':<6} {'Buy USD':<10} {'Buy ALKIMI':<14} {'Sell USD':<10} {'Sell ALKIMI':<14}")
    print(f"    {'─' * 54}")
    for i in range(5):
        print(f"    {i:<6} {fmt_usd(buy_usd[i]):<10} {buy_tokens[i]:>10,.0f}      {fmt_usd(sell_usd[i]):<10} {sell_tokens[i]:>10,.0f}")

    print(f"\n    Buy total:  {fmt_usd(sum(buy_usd))} ({sum(buy_tokens):,.0f} ALKIMI)")
    print(f"    Sell total: {fmt_usd(sum(sell_usd))} ({sum(sell_tokens):,.0f} ALKIMI)")

    # --- Summary ---
    print("\n" + "=" * 70)
    print("  ORDER GRID SUMMARY")
    print("=" * 70)
    print(f"  Mid-price:       {fmt_price(global_mid)}")
    print(f"  Volatility:      {vol:.6f} (simple)  /  {zz_vol:.6f} (ZZ, {zz_regime})")
    print(f"  Aggressiveness:  {agg:.4f} (base)  /  buy={buy_agg:.4f}  sell={sell_agg:.4f}")
    print(f"  Skew:            {skew:.4f} (token drift {state.token_drift_pct:+.1f}%)")
    print(f"  Buy budget:      {fmt_usd(sum(buy_usd))} across 5 levels")
    print(f"  Sell budget:     {fmt_usd(sum(sell_usd))} across 5 levels")
    print(f"  Tightest buy:    {fmt_price(buy_prices[0])} ({fmt_pct(buy_spreads[0])} from mid)")
    print(f"  Tightest sell:   {fmt_price(sell_prices[0])} ({fmt_pct(sell_spreads[0])} from mid)")
    print(f"  Widest buy:      {fmt_price(buy_prices[-1])} ({fmt_pct(buy_spreads[-1])} from mid)")
    print(f"  Widest sell:     {fmt_price(sell_prices[-1])} ({fmt_pct(sell_spreads[-1])} from mid)")
    print(f"\n  Mode: DRY RUN — no orders placed")
    print("=" * 70)


if __name__ == "__main__":
    main()
