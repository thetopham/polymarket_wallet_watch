# PRD and Implementation Plan: Inventory-Aware Passive Market Maker for Polymarket BTC 15m

## Goal

Build a read-only/replay-first emulator for an inventory-aware passive market-making strategy on Polymarket BTC 15m Up/Down markets.

This is not an indicator bot and not a simple directional BTC up/down predictor. The core thesis is:

```text
matched_pair_cost = YES_avg + NO_avg

if matched_pair_cost < 1.00:
    matched YES/NO inventory has positive settlement value if held to resolution
```

The strategy tries to create positive-edge matched inventory through passive bid placement, inventory balancing, and repair logic while keeping unpaired directional exposure bounded.

The product should first reconstruct and replay the observed focus-wallet behavior, then provide a safer emulator that preserves the good parts and blocks the bad parts.

## Current Context

Repository: `/home/matt/polymarket_wallet_watch`

Current working assumptions:

- Safety boundary is read-only/replay-first.
- No private keys.
- No authenticated order submission.
- No live trading.
- All strategy work must use local wallet fills, local Polymarket 1s orderbook feed, and local SQLite artifacts.

Existing useful pieces:

- Wallet ingestion:
  - `src/polymarket_wallet_watch/ingest_wallets.py`
- Polymarket-native 1s orderbook adapter:
  - `src/polymarket_wallet_watch/adapter_polymarket_1s.py`
- Enrichment using local feed:
  - `src/polymarket_wallet_watch/enrich_market_state.py`
- Passive pair-builder research scaffolding:
  - `src/polymarket_wallet_watch/passive_pair_builder.py`
- Focus-wallet BTC 15m deconstruction:
  - `src/polymarket_wallet_watch/btc15m_focus_deconstruction.py`
- Local Polymarket 1s feed:
  - `/home/matt/workspace/kalshi-btc-15m-bot/feed/polymarket-btc-1s.sqlite3`
- Wallet DB:
  - `data/polymarket_wallet_watch.sqlite3`

Latest observed evidence from deconstruction:

- Focus wallet: `0xce25e214d5cfe4f459cf67f08df581885aae7fdc`
- Scope: BTC 15m only
- Markets analyzed: 10
- Fills analyzed: 446
- Paired markets: 10
- Sub-1.00 pair markets: 6/10
- Sub-0.95 pair markets: 3/10
- Passive-looking near-bid fills: about 60%
- Rough reconstructed matched edge if held: positive
- Strong evidence of pair inventory construction, passive fills, late repair, and unpaired strong-side remnants

Core strategy insight:

- Strong side inventory acts as the anchor.
- Weak side is accumulated as hedge/repair.
- Weak side should generally not exceed strong side.
- Matched inventory is the goal.
- Unpaired inventory is controlled directional exposure.
- The system is likely directional bias + volatility harvesting + cheap hedge accumulation, not pure market neutral.

## Product Requirements

### PRD 1: Strategy Objective

The emulator must attempt to construct positive-edge matched YES/NO inventory in BTC 15m markets using passive bids.

Primary success metric:

```text
matched_pair_cost = YES_avg + NO_avg < 1.00
```

Preferred target:

```text
matched_pair_cost <= 0.95
```

Hard safety ceiling:

```text
matched_pair_cost <= 0.99 for repair fills
matched_pair_cost > 1.00 generally blocked unless explicitly classified as capped directional exposure
```

### PRD 2: Inventory Model

The system must maintain exact reconstructed inventory state per market:

- YES qty
- YES notional/cost
- YES avg
- NO qty
- NO notional/cost
- NO avg
- matched pair qty
- matched pair cost
- locked edge if held
- unpaired side
- unpaired qty
- unpaired notional
- total notional
- max inventory swing
- realized reductions/sells if any

Definitions:

```python
matched_qty = min(yes_qty, no_qty)
yes_avg = yes_cost / yes_qty
no_avg = no_cost / no_qty
matched_pair_cost = yes_avg + no_avg
locked_edge_if_held = matched_qty * (1 - matched_pair_cost)
```

### PRD 3: Strong/Weak Side Logic

The system must classify the current market side state:

- strong side: side currently more likely / more expensive / closer to winning
- weak side: side currently cheaper / losing / hedge side

Initial simple logic:

```text
if YES mid > NO mid: strong = YES, weak = NO
if NO mid > YES mid: strong = NO, weak = YES
```

Later extensions:

- BTC price relative to strike / price-to-beat
- BTC slope/velocity
- seconds to close
- orderbook imbalance
- phase-specific confidence

### PRD 4: Balance Rules

The strategy brain is `inventory_balance_rules.py`.

Core rule:

```python
weak_qty <= strong_qty
```

Candidate fill is allowed only if at least one safe condition holds:

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

Block if:

- weak side after fill exceeds strong side after fill
- projected pair cost exceeds hard ceiling
- unpaired strong exceeds cap
- unpaired weak exceeds cap
- visible depth too low
- orderbook stale
- too close to close except repair-only mode
- fill would worsen pair cost without improving balance

### PRD 5: Passive Ladder Quote Planning

The system must generate passive quote intents, not real orders.

At market open:

- place or simulate ladders on both YES and NO
- quote around noisy initial price levels
- use conservative size
- do not assume fills unless replay confirms fills under fill simulator assumptions

Example open ladder:

```yaml
open_ladder:
  enabled: true
  start_seconds_after_open: 0
  end_seconds_after_open: 180
  levels:
    - offset_from_mid: 0.02
      size: 25
    - offset_from_mid: 0.05
      size: 50
    - offset_from_mid: 0.10
      size: 75
    - offset_from_mid: 0.15
      size: 100
```

Weak-side repair ladder:

```yaml
repair_ladder:
  enabled: true
  levels:
    - max_price: 0.30
      size: 50
    - max_price: 0.20
      size: 100
    - max_price: 0.12
      size: 150
    - max_price: 0.06
      size: 200
```

### PRD 6: Risk Limits

Required configurable limits:

```yaml
target_pair_cost: 0.95
hard_pair_cost_ceiling: 0.99
absolute_pair_cost_ceiling: 1.00
max_weak_to_strong_ratio: 1.0
max_unpaired_strong_qty: 300
max_unpaired_weak_qty: 0
max_total_contracts: 2500
max_pair_notional: 2000
max_unpaired_notional: 300
max_fill_count_per_market: 150
min_visible_depth: 25
stale_book_seconds: 2
repair_only_seconds_before_close: 180
stop_new_seed_seconds_before_close: 300
```

### PRD 7: Replay and Fill Realism

Replay is the truth test.

The replay engine must avoid fantasy passive-MM PnL.

Rules:

- no lookahead
- next-tick fills only
- quote must exist before the book touches/crosses it
- conservative queue assumptions
- visible depth cap
- partial fills supported
- stale book blocks fills
- no fills from future orderbook states
- all blocked/missed fills must have explicit reasons

Key warning:

```text
wallet filled near bid != emulator would have filled near bid
```

The system must model the difference.

### PRD 8: Settlement Calculator

The system must compute settlement-ready PnL, not only pair edge.

Required outputs per market:

- winning side
- result source:
  - official settlement
  - local resolved market
  - Chainlink/BTC proxy
  - final snapshot proxy
  - unknown
- YES payout
- NO payout
- matched-pair payout
- unpaired payout
- matched-pair PnL
- unpaired directional PnL
- total PnL
- ROI
- unresolved flag

Must clearly separate:

- reconstructed edge
- proxy settlement
- official realized PnL

### PRD 9: Wallet-vs-Emulator Comparison

The system must compare observed focus-wallet behavior against emulator behavior.

Metrics:

- wallet fills vs emulator fills
- wallet pair cost vs emulator pair cost
- wallet matched qty vs emulator matched qty
- wallet unpaired exposure vs emulator unpaired exposure
- wallet passive-fill share vs emulator fill assumption share
- wallet PnL proxy vs emulator PnL proxy
- missed fill reasons
- blocked wallet fills that emulator correctly avoided
- emulator fills the wallet did not take

## Minimal Viable Core Modules

### 1. `src/polymarket_wallet_watch/inventory_state.py`

Purpose:

Track exact YES/NO inventory state and projected state after candidate fills.

Core classes:

```python
@dataclass
class Fill:
    ts: str
    market_slug: str
    side: Literal["YES", "NO"]
    action: Literal["buy", "sell"]
    price: float
    size: float
    source: str | None = None

@dataclass
class InventoryState:
    yes_qty: float
    yes_cost: float
    no_qty: float
    no_cost: float
```

Core methods:

- `apply_fill(fill: Fill) -> None`
- `project_fill(fill: Fill) -> InventoryState`
- `yes_avg -> float | None`
- `no_avg -> float | None`
- `matched_qty -> float`
- `matched_pair_cost -> float | None`
- `locked_edge_if_held -> float | None`
- `unpaired_side -> str | None`
- `unpaired_qty -> float`
- `unpaired_notional -> float`
- `to_snapshot() -> dict`

Tests:

- applying YES and NO buys computes pair cost
- sells reduce inventory at average cost
- matched qty and unpaired qty correct
- projected fill does not mutate original
- locked edge positive when pair cost below 1

### 2. `src/polymarket_wallet_watch/pair_cost_engine.py`

Purpose:

Centralize pair-cost and edge math.

Functions:

- `weighted_avg(cost, qty)`
- `matched_qty(yes_qty, no_qty)`
- `pair_cost(yes_avg, no_avg)`
- `locked_edge(matched_qty, pair_cost)`
- `projected_pair_cost_after_fill(inventory, side, price, size)`
- `pair_cost_improvement(before, after)`

Tests:

- pair edge exact math
- handles missing/zero qty
- improvement/worsening classification

### 3. `src/polymarket_wallet_watch/inventory_balance_rules.py`

Purpose:

The strategy brain. Decide whether a candidate fill/quote is allowed.

Core classes:

```python
@dataclass
class BalanceRuleConfig:
    target_pair_cost: float = 0.95
    hard_pair_cost_ceiling: float = 0.99
    absolute_pair_cost_ceiling: float = 1.00
    max_weak_to_strong_ratio: float = 1.0
    max_unpaired_strong_qty: float = 300
    max_unpaired_weak_qty: float = 0
    max_pair_notional: float = 2000
    min_visible_depth: float = 25
    stale_book_seconds: float = 2
```

Outputs:

```python
@dataclass
class BalanceDecision:
    allowed: bool
    reason: str
    side: str
    projected_pair_cost: float | None
    projected_matched_qty: float
    projected_unpaired_side: str | None
    projected_unpaired_qty: float
    tags: list[str]
```

Rules:

- allow target pair fills
- allow repair fills under hard ceiling
- block weak side exceeding strong side
- cap unpaired strong side
- block stale/no-depth books
- tag decisions with reason codes

Reason codes:

- `target_pair_cost_ok`
- `repair_under_hard_ceiling`
- `weak_exceeds_strong`
- `pair_cost_above_ceiling`
- `max_unpaired_strong_qty`
- `max_unpaired_weak_qty`
- `stale_book`
- `insufficient_depth`
- `directional_add_capped`

Tests:

- weak side cannot exceed strong side
- repair below hard ceiling allowed
- repair above hard ceiling blocked
- strong side capped by unpaired limit
- target pair cost allowed even if not repair

### 4. `src/polymarket_wallet_watch/ladder_quote_planner.py`

Purpose:

Create passive quote intents from inventory, orderbook, and balance rules.

Core classes:

```python
@dataclass
class LadderLevel:
    price: float | None = None
    offset_from_mid: float | None = None
    size: float = 0

@dataclass
class QuoteIntent:
    market_slug: str
    side: str
    price: float
    size: float
    reason: str
    projected_pair_cost: float | None
    blocked: bool
    blocked_reason: str | None
```

Planner behavior:

- open phase: quote both sides conservatively
- mid phase: quote underweight/cheap side first
- late phase: repair only
- strong-side adds are capped
- quote prices must be passive, not crossing ask
- all quote intents must pass `inventory_balance_rules`

Tests:

- open creates both-side ladder
- weak repair ladder respects weak <= strong
- late phase blocks new seed and allows repair
- blocked quotes include reason

### 5. `src/polymarket_wallet_watch/passive_fill_simulator.py`

Purpose:

Replay quote intents against local 1s orderbook realistically.

Core classes:

```python
@dataclass
class RestingQuote:
    quote_id: str
    market_slug: str
    side: str
    price: float
    remaining_size: float
    placed_ts: str
    expires_ts: str | None

@dataclass
class SimulatedFill:
    quote_id: str
    ts: str
    side: str
    price: float
    size: float
    fill_reason: str
```

Fill rules:

- quote must be placed before fill tick
- next-tick only
- fill only if book touches/crosses quote
- partial fills based on visible depth and queue haircut
- stale snapshots ignored
- quotes can cancel/replace

Config:

```yaml
queue_depth_haircut: 0.25
max_fill_fraction_of_visible_depth: 0.25
quote_ttl_seconds: 30
replace_if_price_moves_ticks: 2
```

Tests:

- no same-tick lookahead fill
- next-tick touch fills partially
- visible depth caps fill size
- stale book blocks fill
- quote expiration prevents later fill

### 6. `src/polymarket_wallet_watch/settlement_calculator.py`

Purpose:

Compute payout/PnL by market.

Core classes:

```python
@dataclass
class SettlementResult:
    market_slug: str
    winning_side: str | None
    result_source: str
    yes_payout: float
    no_payout: float
    matched_pair_pnl: float
    unpaired_pnl: float
    total_pnl: float
    roi: float | None
    provisional: bool
```

Result sources:

- `official_polymarket`
- `local_market_closed`
- `chainlink_proxy`
- `final_snapshot_proxy`
- `unknown`

Tests:

- matched YES/NO pair pays 1 per pair
- unpaired winning side pays 1
- unpaired losing side pays 0
- unresolved markets marked provisional

## Support Modules

### `src/polymarket_wallet_watch/market_window.py`

Purpose:

Parse BTC 15m market windows and phase timing.

Functions:

- `parse_btc_15m_slug(slug)`
- `seconds_after_open(ts, slug)`
- `seconds_before_close(ts, slug)`
- `phase_for_ts(ts, slug)`

### `src/polymarket_wallet_watch/side_strength.py`

Purpose:

Determine strong/weak side.

Initial version:

- strong side = side with higher mid
- weak side = other side

Later:

- add BTC price/strike distance
- add slope/velocity
- add seconds-to-close confidence

### `src/polymarket_wallet_watch/wallet_vs_strategy_report.py`

Purpose:

Compare focus wallet to emulator.

Outputs:

- wallet vs emulator fill count
- wallet vs emulator pair cost
- wallet vs emulator matched qty
- wallet vs emulator PnL proxy
- blocked wallet fill reasons
- missed wallet fills
- strategy config summary

## Implementation Plan

### Phase 0: Guardrails and Test Data

1. Keep all new strategy code read-only/replay-only.
2. Do not add authenticated Polymarket clients.
3. Do not add order submission routes.
4. Keep generated reports under `data/reports/`, ignored by git except `.gitkeep`.
5. Use existing focus-wallet BTC 15m artifacts as test/reference data.

Validation:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m polymarket_wallet_watch.btc15m_focus_deconstruction --config config.yaml --write-artifacts --out-dir data/reports
```

### Phase 1: Extract Pure Inventory and Pair Math

Files likely to change/add:

- Add `src/polymarket_wallet_watch/pair_cost_engine.py`
- Add `src/polymarket_wallet_watch/inventory_state.py`
- Add `tests/test_pair_cost_engine.py`
- Add `tests/test_inventory_state.py`
- Refactor `src/polymarket_wallet_watch/btc15m_focus_deconstruction.py` to use `InventoryState`

Steps:

1. Implement pair-cost helper functions.
2. Implement `InventoryState` and `Fill` dataclasses.
3. Add unit tests for buy/sell/projected fill math.
4. Replace local duplicated inventory math in `btc15m_focus_deconstruction.py` with `InventoryState`.
5. Re-run existing deconstruction and confirm outputs are materially unchanged.

Acceptance criteria:

- Unit tests pass.
- Existing BTC 15m report still produces same market-level pair costs within rounding tolerance.
- No strategy behavior yet; just math extraction.

### Phase 2: Implement Balance Rules Brain

Files likely to change/add:

- Add `src/polymarket_wallet_watch/inventory_balance_rules.py`
- Add `tests/test_inventory_balance_rules.py`

Steps:

1. Define `BalanceRuleConfig`.
2. Define `BalanceDecision`.
3. Implement strong/weak quantity constraints.
4. Implement target pair cost and hard ceiling logic.
5. Implement unpaired strong and weak caps.
6. Implement stale-book and visible-depth checks.
7. Add reason codes and tags.

Acceptance criteria:

- Weak side > strong side blocked.
- Repair under hard ceiling allowed.
- Repair above hard ceiling blocked.
- Strong-side directional add capped.
- Every decision returns explicit reason.

### Phase 3: Implement Market Window and Side Strength

Files likely to change/add:

- Add `src/polymarket_wallet_watch/market_window.py`
- Add `src/polymarket_wallet_watch/side_strength.py`
- Add `tests/test_market_window.py`
- Add `tests/test_side_strength.py`

Steps:

1. Extract BTC 15m slug parsing from deconstruction.
2. Add phase calculation.
3. Add simple strong/weak side classifier from book mids.
4. Add optional BTC/strike fields for future classifier.

Acceptance criteria:

- Correct open/close timestamps for `btc-updown-15m-<unix>`.
- Correct open/mid/late phase.
- Strong side follows higher mid.

### Phase 4: Implement Ladder Quote Planner

Files likely to change/add:

- Add `src/polymarket_wallet_watch/ladder_quote_planner.py`
- Add `tests/test_ladder_quote_planner.py`

Steps:

1. Define ladder config dataclasses.
2. Generate open-phase both-side ladders.
3. Generate mid-phase underweight/weak-side repair ladders.
4. Generate late-phase repair-only ladders.
5. Pass every quote through `inventory_balance_rules`.
6. Ensure blocked quote intents include reason.

Acceptance criteria:

- Open phase emits both YES and NO passive quotes.
- Mid phase prefers underweight/weak side.
- Late phase blocks new seed and allows repair only.
- Quotes that would violate weak <= strong are blocked.
- Quotes above pair-cost ceiling are blocked.

### Phase 5: Implement Passive Fill Simulator

Files likely to change/add:

- Add `src/polymarket_wallet_watch/passive_fill_simulator.py`
- Add `tests/test_passive_fill_simulator.py`

Steps:

1. Define resting quote lifecycle.
2. Place quote intents into simulated book.
3. Use 1s snapshots in chronological order.
4. Fill only on next tick or later.
5. Fill only if book touches/crosses quote.
6. Apply visible-depth haircut.
7. Support partial fills.
8. Cancel/expire quotes.
9. Persist optional debug events in memory first; SQLite later.

Acceptance criteria:

- No same-tick lookahead fills.
- Next-tick fill works when touched.
- Partial fills respect visible depth and queue haircut.
- Expired quotes do not fill.
- Replay reports missed-fill/block reasons.

### Phase 6: Implement Settlement Calculator

Files likely to change/add:

- Add `src/polymarket_wallet_watch/settlement_calculator.py`
- Add `tests/test_settlement_calculator.py`

Steps:

1. Implement payout math from `InventoryState`.
2. Add result source enum/string.
3. Support official result when available.
4. Support BTC price/strike proxy as provisional result.
5. Output matched/unpaired/total PnL separately.

Acceptance criteria:

- Matched pair payout correct.
- Unpaired winning/losing payout correct.
- Unknown result marked unresolved/provisional.
- PnL decomposition clear.

### Phase 7: Build Emulator Orchestrator

Files likely to change/add:

- Add `src/polymarket_wallet_watch/passive_mm_emulator.py`
- Add `tests/test_passive_mm_emulator.py`

Steps:

1. Load BTC 15m snapshots by market.
2. Initialize empty inventory at market open.
3. For each tick:
   - update side strength
   - generate quote intents
   - simulate fills from existing resting quotes
   - apply fills to inventory
   - cancel/replace stale quotes
4. At market close, settle or mark unresolved.
5. Return market-level replay result.

Acceptance criteria:

- Can replay one BTC 15m market from local 1s feed.
- Produces quote/fill/inventory timeline.
- Produces settlement-ready result.
- No network calls.
- No order submission.

### Phase 8: Wallet-vs-Strategy Comparison

Files likely to change/add:

- Add `src/polymarket_wallet_watch/wallet_vs_strategy_report.py`
- Add `tests/test_wallet_vs_strategy_report.py`
- Extend `btc15m_focus_deconstruction.py` or keep separate CLI

Steps:

1. Load wallet deconstruction for market.
2. Run emulator for same market.
3. Compare:
   - fills
   - matched qty
   - pair cost
   - unpaired exposure
   - rough PnL/edge
   - passive share
4. Report differences and missed-fill reasons.

Acceptance criteria:

- Produces per-market comparison table.
- Identifies wallet fills emulator avoided due to risk rules.
- Identifies wallet fills emulator missed due to conservative fill assumptions.
- Identifies whether emulator improved pair cost / reduced bad markets.

## Proposed CLI Commands

### Deconstruct observed wallet

```bash
.venv/bin/python -m polymarket_wallet_watch.btc15m_focus_deconstruction \
  --config config.yaml \
  --write-artifacts \
  --out-dir data/reports
```

### Replay emulator for one market

```bash
.venv/bin/python -m polymarket_wallet_watch.passive_mm_emulator \
  --config config.yaml \
  --market btc-updown-15m-1779145200 \
  --out-dir data/reports/replays
```

### Compare wallet vs emulator

```bash
.venv/bin/python -m polymarket_wallet_watch.wallet_vs_strategy_report \
  --config config.yaml \
  --wallet 0xce25e214d5cfe4f459cf67f08df581885aae7fdc \
  --market btc-updown-15m-1779145200 \
  --out-dir data/reports/comparisons
```

## Tests and Validation

Run all tests:

```bash
.venv/bin/python -m pytest -q
```

Run focused tests:

```bash
.venv/bin/python -m pytest \
  tests/test_inventory_state.py \
  tests/test_pair_cost_engine.py \
  tests/test_inventory_balance_rules.py \
  tests/test_ladder_quote_planner.py \
  tests/test_passive_fill_simulator.py \
  tests/test_settlement_calculator.py \
  -q
```

Replay smoke test:

```bash
.venv/bin/python -m polymarket_wallet_watch.passive_mm_emulator \
  --config config.yaml \
  --market btc-updown-15m-1779145200 \
  --max-ticks 120
```

Data sanity checks:

- orderbook snapshots exist for market
- no stale snapshots used for fills
- every fill has a preceding quote
- every fill is next-tick or later
- partial fill sizes do not exceed visible-depth haircut
- pair cost and inventory snapshots are monotonic with applied fills
- no negative inventory unless shorting is explicitly modeled, which it should not be in v1

## Risks and Tradeoffs

### Risk 1: Passive fill fantasy PnL

If fill simulation is too generous, replay will look profitable but fail live/paper.

Mitigation:

- next-tick fills only
- visible-depth haircut
- queue haircut
- partial fills
- conservative missed-fill reporting

### Risk 2: Public wallet fills may be incomplete

Wallet event feed may miss private transfers, claims, merged positions, or non-trade settlement actions.

Mitigation:

- label wallet inventory as reconstructed
- add settlement/claim reconciliation later
- avoid official PnL claims until reconciled

### Risk 3: Strategy is not truly market neutral

Observed wallet may intentionally keep directional exposure.

Mitigation:

- decompose PnL into matched-pair PnL and unpaired directional PnL
- cap unpaired strong side
- block unpaired weak side
- report directional exposure explicitly

### Risk 4: Late repair can become chasing

Late fills may repair pair cost or may add expensive risk near resolution.

Mitigation:

- repair-only late mode
- hard pair-cost ceiling
- weak <= strong rule
- max late fill count/notional

### Risk 5: Overfitting to one focus wallet/day

The strategy may fit observed markets but fail more broadly.

Mitigation:

- replay across multiple days/windows
- separate train/validation market windows
- report robustness by phase/regime
- compare against simple baselines

## Open Questions

1. What should the initial target be: 0.95 or 0.97?
2. Should hard ceiling be 0.99 or exactly 1.00?
3. How much unpaired strong-side exposure is acceptable?
4. Should weak side ever exceed strong side when price is extremely cheap, e.g. < 0.05?
5. How conservative should queue haircut be: 10%, 25%, or 50% of visible depth?
6. Should open ladder be centered around midpoint, bid, or fixed prices like 0.45/0.40/0.35?
7. Should strong-side detection use only orderbook mids initially or BTC strike distance too?
8. How should settlement be sourced for old markets where official resolution is not in local DB?

## Recommended Next Step

Implement Phase 1 and Phase 2 first:

1. `pair_cost_engine.py`
2. `inventory_state.py`
3. `inventory_balance_rules.py`

Then refactor `btc15m_focus_deconstruction.py` to use those modules.

This gives the project a clean strategy brain and inventory math foundation before building replay complexity.

Do not start with live/paper orders. The next milestone is a trustworthy offline emulator that can prove whether the wallet-like ladder can survive conservative passive-fill assumptions.
