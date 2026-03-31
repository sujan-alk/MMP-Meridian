# Quant Model — Huy's Meta Config V1

A detailed technical reference for the aggressiveness-based, volatility-driven order book depth strategy.

---

## Overview

The model answers two questions on every tick:

1. **Where to place orders?** → SpreadEngine: power-curve distribution of bid/ask spreads
2. **How much to place?** → DepthEngine: passive/equal blend of USD amounts per level

Both are driven by a single scalar — **aggressiveness** ∈ [0, 1] — which is derived from rolling market volatility.

---

## 1. Volatility

### Rolling Standard Deviation

Primary volatility measure used in Phase 1.

```
window: 10 minutes of price observations (sampled each second)
returns[i] = (price[i] - price[i-1]) / price[i-1]
vol = std(returns)
```

- **Source**: `quant/volatility.py → VolatilityEngine.rolling_vol()`
- **Window**: configurable via `bot.json → volatility.window_minutes` (default 10)
- **Warm-up**: returns 0.0 until at least 2 observations are available

### Zhang-Zhang (2018) OHLCV Volatility *(Phase 1b)*

A more robust estimator that incorporates intraday high/low data. Less noisy than close-to-close.

```
For each candle (O, H, L, C) using log prices:
  σ²_candle = 0.5 × (ln(H/L))² − (2ln2 − 1) × (ln(C/O))²

vol_zz = sqrt( mean(σ²_candle) )
```

The log(H/L) term captures intraday range; the correction term `(2ln2−1) × (ln(C/O))²` removes the drift component, making the estimator unbiased even in trending markets.

#### Regime Detection

```
net_direction = mean(ln(C/O))   # across OHLCV window

regime = "trending_up"   if net_direction >  0.0005
regime = "trending_down" if net_direction < -0.0005
regime = "choppy"        otherwise
```

Regime feeds into **per-side aggressiveness** in Phase 1b:
- `trending_down` → buy aggressiveness increased (accumulate on dips)
- `trending_up` → sell aggressiveness can be more conservative

---

## 2. Aggressiveness

Converts volatility into a single control parameter ∈ [0, 1].

```
vol ≤ low_threshold (0.001)
    → aggressiveness = 1.0                                  (fully aggressive)

vol ∈ [low_threshold, high_threshold] (0.001 → 0.003)
    → aggressiveness = 1 − ((vol − low_threshold) / (high_threshold − low_threshold)) ^ power

vol ≥ high_threshold (0.003)
    → aggressiveness = 0.0                                  (fully passive)
```

Default `power = 2.0` (quadratic), configurable via `bot.json → volatility.power`.

| vol | aggressiveness | Market State |
|-----|----------------|--------------|
| 0.0010 | 1.00 | Ultra-calm — trade hard |
| 0.0015 | 0.94 | Quiet |
| 0.0020 | 0.75 | Moderate vol |
| 0.0025 | 0.44 | Elevated vol |
| 0.0030 | 0.00 | High vol — protect positions |

**Interpretation:**
- `1.0` → Tight spreads, equal-size depth distribution → maximise fill rate
- `0.0` → Wide spreads, front-loaded depth → protect against book sweeps

---

## 3. Spread Engine

### Principle

At `agg = 1`: all 15 levels cluster near the mid-price (tight spreads).
At `agg = 0`: all 15 levels cluster near the widest configured spread.

### Formula

```python
t = linspace(0, 1, n_levels)          # [0, 0.07, 0.14, ..., 1.0]

gamma = exp(curve_strength × (1.0 − 2.0 × aggressiveness))

# When agg = 1: gamma = exp(4×(1-2)) = exp(-4) ≈ 0.018 → t^gamma ≈ 1 (levels push to widest end)
# When agg = 0: gamma = exp(4×(1-0)) = exp(4)  ≈ 54.6 → t^gamma ≈ 0 (levels cluster at min)

spread_i = tightest + (widest − tightest) × t[i] ^ gamma
```

**Wait — why does `agg=1` push toward widest?**

For **buy** side: `tightest = -0.1%`, `widest = -5.0%`. In absolute terms, "widest" means furthest from mid. When `agg = 1`, the power curve *compresses* levels toward the tightest end of the parameter range, which for buy orders is near -0.1% (closest to mid). The formula achieves this because at high aggressiveness, gamma is small, and `t^gamma → 1` uniformly, spacing levels evenly. Actually the formula causes tight clustering at one end depending on sign — see the code for the exact parameterisation.

### Default Spread Ranges

| Side | Tightest (closest to mid) | Widest (furthest from mid) |
|------|--------------------------|---------------------------|
| Buy  | −0.1% | −5.0% |
| Sell | +0.3% | +7.0% |

Configurable per exchange in `bot.json → exchanges[*].spread`.

### Example: 5 levels at different aggressiveness

```
Sell side (sell_min=0.3%, sell_max=7.0%, n=5):

agg = 1.0 (gamma ≈ 0.018):
  Level 1: +0.30%   Level 2: +0.30%   Level 3: +0.30%   Level 4: +0.30%   Level 5: +0.31%
  → All orders cluster just above mid

agg = 0.5 (gamma = 1.0):
  Level 1: +0.30%   Level 2: +2.02%   Level 3: +3.75%   Level 4: +5.47%   Level 5: +7.00%
  → Even distribution across full range

agg = 0.0 (gamma ≈ 54.6):
  Level 1: +6.80%   Level 2: +6.93%   Level 3: +6.98%   Level 4: +7.00%   Level 5: +7.00%
  → All orders cluster at widest spread
```

---

## 4. Depth Engine

### Principle

At `agg = 1`: equal-size orders at every level → incentivises trading.
At `agg = 0`: heavy size at innermost levels → guards against sweep attacks.

### Formula

```python
# Two anchor distributions (normalised to sum to 1.0):
passive = geometric_decay(n, ratio=0.8)   # e.g. [0.35, 0.28, 0.22, 0.18, 0.14, ...] normalised
equal   = [1/n, 1/n, ..., 1/n]

# Blend factor: 0 at agg=0, 1 at agg=1 (sharpened by curve_strength)
blend = aggressiveness ^ curve_strength   # 0^4=0, 0.5^4=0.0625, 1^4=1

# Weighted blend
proportions = (1 − blend) × passive + blend × equal

# Apply total budget × skew_factor
total_usd = config.depth.total_budget_usd
buy_budget  = total_usd × min(skew_factor, 2.0)    / 2   # extra buy if under-token
sell_budget = total_usd × min(2/skew_factor, 2.0)  / 2   # extra sell if over-token

amounts_usd[i] = proportions[i] × budget_for_side
```

### Geometric Decay

The "passive" distribution uses:
```
weights[i] = ratio^i  (before normalisation)
```

With `ratio = 0.8`: level 1 gets ~35% of budget, level 2 ~28%, level 3 ~22%, etc.

This means the innermost orders are large — they absorb sweep volume without depleting all capital.

### Skew Factor

The skew factor biases buy vs sell budgets based on inventory drift.

```
token_drift = (initial_token − current_token) / initial_token

raw_skew = 1.0 − (token_drift × 2.0)
skew_factor = clamp(raw_skew, 0.5, 2.0)
```

| Token drift | Skew | Effect |
|-------------|------|--------|
| -20% (over-token) | 0.60 | Reduce buy budget, increase sell |
| 0% (neutral) | 1.00 | Equal buy/sell budget |
| +10% (under-token) | 1.20 | Increase buy budget |
| +25% (under-token) | 1.50 | Significantly more buying |

The 10% rebalance threshold (`should_rebalance()`) triggers a log warning when drift exceeds this.

---

## 5. Order Grid Construction

Each tick, the bot builds a complete desired order grid:

```
For each side (buy / sell):
  spreads[i]    = SpreadEngine.compute_levels(agg, n)
  amounts_usd[i] = DepthEngine.compute_amounts(agg, n, skew, side)
  price[i]      = global_mid × (1 + spreads[i] / 100)
  amount_token[i] = amounts_usd[i] / price[i]
```

The grid is a list of `(price, amount_token, side)` tuples — the desired state of the order book.

---

## 6. Diff-and-Repost

The `OrderManager` compares the desired grid against current open orders:

```
PRICE_TOLERANCE_PCT = 0.05%

For each open order:
  Find closest desired price
  If |open_price − desired_price| / desired_price > 0.0005:
    → Mark for cancellation

For each desired level:
  If no open order within tolerance:
    → Mark for placement
```

**Why 0.05%?** This is below typical tick size spread movement per second. In a stable market, 0 orders need cancelling per tick. In a volatile market, only the most-moved levels get repriced.

**API call savings:** At 15 levels/side × 4 exchanges, a cancel-all approach would fire 120 cancel calls per tick. Diff-and-repost averages ~2-4 cancels per tick in normal conditions.

---

## 7. Global Mid-Price

```
global_mid = 0.45 × Gate + 0.45 × KuCoin + 0.05 × MEXC + 0.05 × Kraken
```

Weights reflect liquidity depth. Gate and KuCoin are the primary price discovery venues for ALKIMI.

**Fallback**: If an exchange fails to respond, weights are normalised over responding exchanges:

```python
available = {ex: w for ex, w in weights.items() if ex in responding}
total = sum(available.values())
normalised = {ex: w / total for ex, w in available.items()}
```

If Gate (0.45) is down: KuCoin becomes ~0.818, MEXC ~0.091, Kraken ~0.091.

---

## 8. Phase 1b Enhancements

These features are scaffolded (code exists) but not yet active:

| Feature | Location | Status |
|---------|----------|--------|
| Zhang-Zhang vol | `quant/volatility.py` | Scaffolded |
| ZZ regime → per-side aggressiveness | `quant/aggressiveness.py → compute_with_regime()` | Scaffolded |
| Fill-rate-adaptive aggressiveness | `core/exchange_bot.py` | Not yet implemented |

To activate Zhang-Zhang: set `orchestrator.py` to call `vol_engine.zhang_zhang_vol()` and pass `(vol, regime)` to `AggressivenessModel.compute_with_regime()`.

---

## 9. RL Agent Interface *(Phase 2)*

The feature collector writes an observation vector every 10 seconds to `rl_features`:

```
ObservationVector:
  vol_simple       ← current rolling volatility
  vol_zz           ← Zhang-Zhang volatility
  zz_regime        ← trending_up | trending_down | choppy
  aggressiveness   ← current aggressiveness scalar
  skew_factor      ← inventory skew
  token_drift_pct  ← % drift from initial token balance
  global_mid       ← current ALKIMI/USD price
  bid_ask_spread_bps ← tightest level spread in basis points
  fill_rate_1m     ← fills per minute (30-tick rolling window)
  pnl_1h           ← unrealised P&L change over last hour
```

The v1 `PassthroughAgent` always returns `aggressiveness_override = None` (no intervention). Phase 2 will train a PPO/SAC agent on this dataset to learn optimal aggressiveness policy.
