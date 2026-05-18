# PRD: Polymarket Wallet Watch

## Product

A read-only analytics pipeline for Polymarket BTC-style prediction markets that tracks selected wallets, detects convergence among profitable wallets, enriches wallet events with market state, and produces research reports suitable for later paper-trading against Kalshi/Polymarket.

## Non-goals

- No live trading.
- No private keys.
- No order submission.
- No automated copy trading.
- No custody or wallet signing.

## Users

A prediction-market researcher/operator who wants to know whether profitable public Polymarket wallets produce post-trade alpha, and whether clusters of wallets have stronger forward performance than individual wallets or simple baselines.

## Data sources

1. Polymarket public APIs
   - Gamma/Data API for markets/events metadata.
   - CLOB public endpoints for orderbooks, prices, spreads, and trades where available.
2. Polygon RPC/indexer
   - Optional read-only settlement/position backfill.
   - Do not use Helius; Polymarket settles on Polygon, not Solana.
3. Local BTC/Kalshi feed
   - SQLite 1s BTC/Kalshi feed when available.
   - Used to enrich each wallet event with BTC price, strike distance, time to close, trend, volatility, spread, liquidity, and imbalance.

## Milestones

### Milestone 1: working ingestion and normalized report

Implemented scope:
- SQLite schema.
- config.example.yaml.
- market ingestion from Polymarket Gamma.
- wallet ingestion for configured wallet list.
- raw JSON logging.
- report command that prints last 100 normalized wallet events.
- tests for schema, normalization, convergence, scoring, and report formatting.

Acceptance:
- `python -m polymarket_wallet_watch.ingest_markets --config config.yaml` stores market metadata.
- `python -m polymarket_wallet_watch.ingest_wallets --config config.yaml` stores normalized wallet_events.
- `python -m polymarket_wallet_watch.report --since 24h` prints readable event rows.

### Milestone 2: enrichment

Scope:
- Join wallet_events to local BTC/Kalshi SQLite feed by event timestamp.
- Add nearest market/orderbook snapshot.
- Populate enriched_wallet_events with BTC price, strike, distance_from_strike, distance_bps, slopes, realized volatility, ATR proxy, spread, top-of-book liquidity, imbalance, yes_mid/no_mid, and time bucket.

Acceptance:
- `enrich_market_state` writes one enriched row per wallet event when feed data exists.
- Missing feed fields are null, not fabricated.
- Enrichment provenance/version is stored.

### Milestone 3: wallet alpha and convergence clusters

Scope:
- Compute 15s/30s/60s/180s forward markouts.
- Compute MFE/MAE and expiry pnl where available.
- Score wallets by win rate, average edge, Sharpe-like score, and regime consistency.
- Flag likely market makers that trade both sides constantly.
- Build convergence clusters for 5s/15s/30s/60s windows.
- Store consensus_score, notional, avg wallet alpha, leader wallet, lag distribution, directional agreement, regime, and forward markouts.

Acceptance:
- `score_wallets` creates wallet_alpha rows.
- `detect_convergence --window 30` creates clusters only when at least N distinct wallets converge on the same market/outcome.
- Leader/follower CSV/table is produced.

### Milestone 4: replay

Scope:
- Generate signals when consensus_score exceeds threshold.
- Add optional filters: seconds_to_close, distance_from_strike, volatility expansion, max spread, min liquidity.
- Simulate taker entry with conservative slippage.
- Simulate passive entry separately.
- Compare against random and simple momentum baselines.
- Output chronological train/test split results.

Acceptance:
- `replay_signals --split-date YYYY-MM-DD` writes signal_replay_results rows.
- Report includes train/test signal counts, net pnl, edge cents, win rate, drawdown, and baselines.

## Database tables

Required tables:
- wallets
- markets
- market_snapshots
- wallet_events
- wallet_positions
- enriched_wallet_events
- wallet_alpha
- convergence_clusters
- leader_follower_edges
- signal_replay_results

Additional table:
- raw_api_responses, for reproducibility.

## Wallet event fields

wallet_events stores:
- wallet_address
- market_id / condition_id / token_id
- event timestamp
- side YES/NO
- action buy/sell/add/reduce/exit
- price
- size
- notional
- inferred aggressor/passive if possible
- tx_hash / trade_id / source
- seconds_to_close
- market slug/title
- outcome

## Consensus formula

    consensus_score = sum(wallet_alpha_weight * direction * size_weight * recency_weight)

Where:
- direction is positive for buying the outcome and negative for selling/reducing/exiting it.
- size_weight defaults to sqrt(size) to avoid one huge trade dominating everything.
- recency_weight decays by configurable half-life within the cluster window.
- wallet_alpha_weight comes from latest wallet_alpha score, defaulting conservatively when unknown.

## Leader/follower logic

For wallets A and B:
- If A repeatedly trades before B in the same market/outcome within a bounded lag, record A→B edge.
- If price moves favorably after A and before/after B, increase influence score in later versions.
- Export as graph-like CSV and persist in leader_follower_edges.

## Safety requirements

- Read-only only.
- No private keys.
- No live orders.
- No automatic trading.
- Store API keys only in .env, never commit.
- .gitignore excludes env, DBs, raw logs, and runs.
- Reports must warn about latency, liquidity, external hedging, and likely market-making.
