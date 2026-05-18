# PRD: Inventory-Aware Passive Market Maker for Polymarket BTC 15m

## 1. Goal

Build a read-only, replay-first emulator for an inventory-aware passive market-making strategy on Polymarket BTC 15m Up/Down markets.

This is not a simple directional BTC up/down bot. The strategy objective is to passively build paired YES/NO inventory where the matched pair has positive settlement value:

```text
matched_pair_cost = YES_avg + NO_avg

if matched_pair_cost < 1.00:
    matched YES/NO inventory has positive settlement edge if held to resolution
```

The hard part is execution realism and inventory control:

- passively resting bids without assuming fantasy fills;
- anchoring on the strong side;
- accumulating the weak side as hedge/repair;
- keeping weak side inventory generally less than or equal to strong side inventory;
- capping unpaired directional exposure;
- settling/replaying outcomes without lookahead.

## 2. Safety Boundary

V1 is strictly read-only/replay-first.

Allowed:

- local Polymarket 1s orderbook feed reads;
- public wallet fill reconstruction;
- local SQLite writes for research artifacts;
- replay and simulated fills;
- reports and CSV/JSON exports.

Not allowed:

- private keys;
- signers;
- authenticated order submission;
- live trading;
- automatic trading.

## 3. Current Evidence

Current focus wallet:

```text
0xce25e214d5cfe4f459cf67f08df581885aae7fdc
```

Current scope:

```text
BTC 15m Polymarket Up/Down markets
```

Recent deconstruction showed:

- 10 BTC 15m markets analyzed;
- 446 fills analyzed;
- 10 paired markets;
- 6/10 markets below 1.00 reconstructed matched pair cost;
- 3/10 markets below 0.95 reconstructed matched pair cost;
- about 60% passive-looking near-bid fills;
- strong evidence of pair inventory construction;
- late repair behavior;
- visible unpaired strong-side remnants.

This is enough evidence to justify building an emulator, but not enough to claim live profitability. The replay/fill-realism layer is the truth test.

## 4. Core Strategy Model

### 4.1 Pair Inventory Model

Core quantities:

```python
matched_qty = min(yes_qty, no_qty)
yes_avg = yes_cost / yes_qty
no_avg = no_cost / no_qty
matched_pair_cost = yes_avg + no_avg
locked_edge_if_held = matched_qty * (1.0 - matched_pair_cost)
```

Interpretation:

- Matched YES/NO inventory pays 1.00 per matched pair at settlement.
- If `YES_avg + NO_avg < 1.00`, matched inventory has positive settlement edge.
- If `YES_avg + NO_avg <= 0.95`, matched inventory is preferred/target edge.
- Unpaired inventory is directional exposure, not paired edge.

### 4.2 Strong/Weak Side Model

The strategy is probably not purely market neutral. It appears to be:

```text
directional bias + volatility harvesting + cheap hedge accumulation
```

Definitions:

- strong side: current expensive/probable side, often the side closer to winning;
- weak side: current cheap/losing side, accumulated as hedge/repair;
- matched inventory: paired YES/NO quantity;
- unpaired strong inventory: controlled directional exposure;
- unpaired weak inventory: usually blocked or tightly capped.

Core balance rule:

```python
weak_qty <= strong_qty
```

Candidate fill rule:

```python
allow_fill = (
    pair_cost_after <= target_pair_cost
    or (
        repair_fill
        and pair_cost_after <= hard_ceiling
        and weak_qty_after <= strong_qty_after
    )
    or (
        strong_side_fill
        and unpaired_strong_after <= max_unpaired_strong_qty
        and pair_cost_after <= strong_side_hard_ceiling
    )
)
```

Default parameters:

```yaml
target_pair_cost: 0.95
hard_pair_cost_ceiling: 0.99
absolute_pair_cost_ceiling: 1.00
max_weak_to_strong_ratio: 1.0
max_unpaired_strong_qty: 300
max_unpaired_weak_qty: 0
```

## 5. Volatility-Distance Probability Model

The inventory strategy needs a lightweight model for how dangerous a side is as expiry approaches.

Core normalized distance model:

```text
z = d / (sigma * sqrt(t))
```

Where:

```text
d = BTC price - strike
t = seconds to expiry
sigma = realized volatility proxy
```

Interpretation:

- `d` measures how far BTC is from the strike / price-to-beat.
- `t` controls how much time remains for BTC to cross back.
- `sigma` estimates near-term realized volatility.
- `z` is a normalized distance-to-strike measure.

High positive `z` means BTC is far above strike relative to remaining volatility/time, so YES is strong and NO is weak.

High negative `z` means BTC is far below strike relative to remaining volatility/time, so NO is strong and YES is weak.

Near-zero `z` means the market is close to strike and both sides are highly volatile.

### 5.1 Required Inputs

For each orderbook tick / candidate fill:

- BTC price;
- strike / price-to-beat;
- seconds to expiry;
- realized volatility proxy;
- YES bid/ask;
- NO bid/ask;
- current inventory state.

### 5.2 Realized Volatility Proxy

Initial proxy can use local Polymarket/BTC 1s feed:

```python
sigma = realized_volatility_btc_60s
```

Candidate implementations:

- standard deviation of 1s BTC returns over last 60s;
- standard deviation of absolute BTC price changes over last 60s;
- ATR-style average absolute move over last 60s;
- fallback to longer 180s window when 60s data is sparse.

The exact unit must match `d` and `t` in the denominator. If `d` is in dollars and `t` is in seconds, then `sigma` should be dollar volatility per sqrt(second), or a calibrated proxy documented as such.

### 5.3 Strategy Use of z

The z-score should inform, not replace, inventory rules.

Potential uses:

- classify strong/weak side;
- determine whether unpaired strong-side exposure is acceptable;
- shrink or block weak-side bids when weak side is too likely to expire worthless unless pair edge is excellent;
- allow stronger repair bids when `abs(z)` is high and weak side is cheap enough;
- reduce open-seed size when `z` is already extreme;
- enter repair-only mode when `abs(z)` and time-to-expiry imply low reversal probability.

Example side classification:

```python
if z > z_strong_threshold:
    strong_side = "YES"
    weak_side = "NO"
elif z < -z_strong_threshold:
    strong_side = "NO"
    weak_side = "YES"
else:
    strong_side = side_with_higher_mid
    weak_side = other_side
```

Example risk adjustment:

```python
if abs(z) < 0.5:
    # near strike: high flip risk, keep inventory balanced
    max_unpaired_strong_qty *= 0.5
elif abs(z) > 2.0 and seconds_to_expiry < 180:
    # likely resolved regime: allow capped strong-side remainder,
    # but only add weak side if pair cost is very cheap
    weak_repair_target_pair_cost = 0.90
```

### 5.4 Acceptance Criteria for z Model

- Computes z for snapshots with BTC price, strike, time-to-expiry, and volatility.
- Returns null/unknown when inputs are missing or invalid.
- Never permits a fill by itself; all fills still pass inventory balance rules.
- Emits model metadata in reports:
  - `d`
  - `t`
  - `sigma`
  - `z`
  - strong side
  - weak side
  - confidence/regime label

## 6. Product Requirements

### PRD 1: Inventory State

The system must maintain exact reconstructed inventory state per market:

- YES qty/cost/avg;
- NO qty/cost/avg;
- matched pair qty;
- matched pair cost;
- matched edge if held;
- unpaired side/qty/notional;
- max inventory swing;
- fill timeline.

### PRD 2: Pair-Cost Optimizer

The system must evaluate every candidate fill by projected pair cost:

- projected pair cost before fill;
- projected pair cost after fill;
- whether fill improves or worsens pair cost;
- whether fill creates or expands matched inventory;
- whether fill creates unpaired directional risk.

### PRD 3: Inventory Balance Rules

The system must enforce:

- weak side generally cannot exceed strong side;
- repair fills allowed under hard ceiling;
- target pair fills allowed when under target cost;
- unpaired strong side capped;
- unpaired weak side blocked or tightly capped;
- all blocked decisions have explicit reason codes.

### PRD 4: Passive Ladder Quote Planner

The system must generate quote intents, not orders.

Phases:

- open: conservative two-sided ladder;
- mid: underweight/weak-side repair and pair-cost optimization;
- late: repair-only mode;
- close: hold/no new risk.

### PRD 5: Replay and Fill Realism

The replay engine must avoid fantasy passive market-making PnL.

Rules:

- no lookahead;
- quote must exist before fill tick;
- next-tick fills only;
- visible depth haircut;
- queue haircut;
- partial fills;
- stale book blocks fills;
- all misses and blocks logged.

Important warning:

```text
wallet filled near bid != emulator would have filled near bid
```

### PRD 6: Settlement Calculator

The system must separate:

- reconstructed matched-pair edge;
- proxy settlement PnL;
- official realized PnL when available.

Per market output:

- winning side;
- result source;
- matched-pair PnL;
- unpaired PnL;
- total PnL;
- ROI;
- provisional/unresolved flag.

### PRD 7: Wallet-vs-Strategy Comparison

The system must compare observed focus-wallet behavior against emulator behavior:

- fill count;
- fill timing;
- pair cost;
- matched qty;
- unpaired exposure;
- passive-fill share;
- missed fills;
- blocked wallet fills;
- emulator PnL vs wallet reconstructed PnL.

## 7. Minimal Core Modules

### 7.1 `inventory_state.py`

Tracks inventory and projected inventory.

Responsibilities:

- apply fills;
- project fills;
- compute averages;
- compute matched pair qty/cost;
- compute unpaired exposure;
- snapshot state.

### 7.2 `pair_cost_engine.py`

Central pair-cost math.

Responsibilities:

- weighted averages;
- matched qty;
- pair cost;
- locked edge;
- pair-cost improvement/worsening.

### 7.3 `inventory_balance_rules.py`

The strategy brain.

Responsibilities:

- enforce weak <= strong;
- enforce pair cost ceilings;
- cap directional remnants;
- block stale/depth-insufficient books;
- emit reason codes.

### 7.4 `ladder_quote_planner.py`

Builds passive quote intents.

Responsibilities:

- open ladder;
- weak-side repair ladder;
- strong-side capped directional ladder;
- late repair-only mode;
- blocked quote explanations.

### 7.5 `passive_fill_simulator.py`

Replays quotes against local 1s orderbooks.

Responsibilities:

- quote lifecycle;
- next-tick fill simulation;
- visible-depth caps;
- queue haircuts;
- partial fills;
- no-lookahead enforcement.

### 7.6 `settlement_calculator.py`

Computes settlement/PnL.

Responsibilities:

- official/proxy result handling;
- matched pair payout;
- unpaired payout;
- total PnL;
- result provenance.

### 7.7 Support Modules

Additional support modules:

- `market_window.py`
- `side_strength.py`
- `volatility_distance_model.py`
- `wallet_vs_strategy_report.py`
- `passive_mm_emulator.py`

## 8. Implementation Plan

### Phase 1: Extract Inventory and Pair Math

Files:

- `src/polymarket_wallet_watch/pair_cost_engine.py`
- `src/polymarket_wallet_watch/inventory_state.py`
- `tests/test_pair_cost_engine.py`
- `tests/test_inventory_state.py`

Acceptance:

- pair-cost math covered by tests;
- inventory projection does not mutate original;
- current deconstruction can be refactored to use shared inventory state.

### Phase 2: Implement Volatility-Distance Model

Files:

- `src/polymarket_wallet_watch/volatility_distance_model.py`
- `tests/test_volatility_distance_model.py`

Acceptance:

- computes `d = btc_price - strike`;
- computes `t = seconds_to_expiry`;
- computes or accepts `sigma` realized volatility proxy;
- computes `z = d / (sigma * sqrt(t))`;
- handles zero/missing sigma and expired markets safely;
- classifies strong/weak side from z and/or mids;
- emits metadata for reports.

### Phase 3: Implement Inventory Balance Rules

Files:

- `src/polymarket_wallet_watch/inventory_balance_rules.py`
- `tests/test_inventory_balance_rules.py`

Acceptance:

- weak side exceeding strong side blocked;
- repair under hard ceiling allowed;
- repair above hard ceiling blocked;
- strong-side directional adds capped;
- z-regime can tighten/loosen caps but never bypass hard risk rules.

### Phase 4: Implement Market Window and Side Strength

Files:

- `src/polymarket_wallet_watch/market_window.py`
- `src/polymarket_wallet_watch/side_strength.py`
- `tests/test_market_window.py`
- `tests/test_side_strength.py`

Acceptance:

- correct BTC 15m open/close parsing;
- correct open/mid/late phase;
- side strength can use mids and z model.

### Phase 5: Implement Ladder Quote Planner

Files:

- `src/polymarket_wallet_watch/ladder_quote_planner.py`
- `tests/test_ladder_quote_planner.py`

Acceptance:

- open phase emits two-sided passive ladder;
- mid phase prefers weak/underweight repair;
- late phase repair-only;
- all quote intents pass balance rules or include blocked reason.

### Phase 6: Implement Passive Fill Simulator

Files:

- `src/polymarket_wallet_watch/passive_fill_simulator.py`
- `tests/test_passive_fill_simulator.py`

Acceptance:

- no same-tick fills;
- next-tick touch/cross fills;
- partial fills respect visible depth and queue haircut;
- expired quotes do not fill;
- stale books block fills.

### Phase 7: Implement Settlement Calculator

Files:

- `src/polymarket_wallet_watch/settlement_calculator.py`
- `tests/test_settlement_calculator.py`

Acceptance:

- matched pair payout correct;
- unpaired payout correct;
- unknown/proxy/official settlement clearly labeled.

### Phase 8: Emulator and Wallet Comparison

Files:

- `src/polymarket_wallet_watch/passive_mm_emulator.py`
- `src/polymarket_wallet_watch/wallet_vs_strategy_report.py`
- `tests/test_passive_mm_emulator.py`
- `tests/test_wallet_vs_strategy_report.py`

Acceptance:

- can replay one BTC 15m market;
- produces quote/fill/inventory timeline;
- compares wallet vs emulator;
- reports misses, blocks, and edge differences.

## 9. Validation Commands

Run all tests:

```bash
.venv/bin/python -m pytest -q
```

Refresh wallet and enrichment data:

```bash
.venv/bin/python -m polymarket_wallet_watch.ingest_wallets --config config.yaml
.venv/bin/python -m polymarket_wallet_watch.enrich_market_state --config config.yaml --limit 5000
```

Re-run focus-wallet deconstruction:

```bash
.venv/bin/python -m polymarket_wallet_watch.btc15m_focus_deconstruction \
  --config config.yaml \
  --write-artifacts \
  --out-dir data/reports
```

Future replay command:

```bash
.venv/bin/python -m polymarket_wallet_watch.passive_mm_emulator \
  --config config.yaml \
  --market btc-updown-15m-1779145200 \
  --out-dir data/reports/replays
```

## 10. Risks

### Passive Fill Fantasy

Risk:

Replay assumes fills that would not happen in real queue position.

Mitigation:

- next-tick only;
- queue haircut;
- partial fills;
- visible depth cap;
- conservative missed-fill reporting.

### Incomplete Wallet Reconstruction

Risk:

Public wallet feed may miss claims, transfers, or complete position state.

Mitigation:

- label as reconstructed;
- add settlement/claim reconciliation later;
- separate official PnL from reconstructed edge.

### Directional Exposure Hidden as Pair Strategy

Risk:

Unpaired strong side drives PnL, not matched pair edge.

Mitigation:

- report matched and unpaired PnL separately;
- cap unpaired exposure;
- block weak > strong.

### Overfitting to One Wallet

Risk:

Rules fit one focus wallet/day but do not generalize.

Mitigation:

- replay across many BTC 15m windows;
- train/validation splits;
- compare against simple baselines.

## 11. Open Questions

1. Should target pair cost be 0.95, 0.97, or dynamic by z-score regime?
2. Should hard ceiling be 0.99 or exactly 1.00?
3. Should weak side ever exceed strong side when weak price is extremely low, e.g. under 0.05?
4. How should z-score regimes modify open ladder size?
5. Which sigma proxy is most stable: 60s realized vol, 180s realized vol, or ATR-style absolute movement?
6. What queue haircut is realistic for Polymarket BTC 15m liquidity?
7. Should late repair become stricter as `abs(z)` increases near expiry?

## 12. Recommended Next Build Step

Implement the foundation first:

1. `pair_cost_engine.py`
2. `inventory_state.py`
3. `volatility_distance_model.py`
4. `inventory_balance_rules.py`

Then refactor `btc15m_focus_deconstruction.py` to use those modules before building the full replay engine.
