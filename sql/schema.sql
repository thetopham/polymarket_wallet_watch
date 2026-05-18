-- Polymarket Wallet Watch read-only research schema
-- SQLite-first; all collectors are observation-only and never store private keys.

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS wallets (
    wallet_address TEXT PRIMARY KEY,
    label TEXT,
    notes TEXT,
    watch_enabled INTEGER NOT NULL DEFAULT 1,
    first_seen_ts TEXT,
    last_seen_ts TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS markets (
    market_id TEXT PRIMARY KEY,
    condition_id TEXT,
    question_id TEXT,
    slug TEXT,
    title TEXT,
    event_slug TEXT,
    category TEXT,
    active INTEGER,
    closed INTEGER,
    start_ts TEXT,
    close_ts TEXT,
    end_ts TEXT,
    strike REAL,
    raw_json TEXT,
    source TEXT NOT NULL DEFAULT 'gamma',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_markets_condition_id ON markets(condition_id);
CREATE INDEX IF NOT EXISTS idx_markets_slug ON markets(slug);
CREATE INDEX IF NOT EXISTS idx_markets_close_ts ON markets(close_ts);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT,
    condition_id TEXT,
    token_id TEXT,
    snapshot_ts TEXT NOT NULL,
    yes_bid REAL,
    yes_ask REAL,
    yes_mid REAL,
    no_bid REAL,
    no_ask REAL,
    no_mid REAL,
    spread REAL,
    top_bid_size REAL,
    top_ask_size REAL,
    top_of_book_liquidity REAL,
    imbalance REAL,
    raw_json TEXT,
    source TEXT NOT NULL DEFAULT 'clob',
    UNIQUE(token_id, snapshot_ts, source)
);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_token_ts ON market_snapshots(token_id, snapshot_ts);

CREATE TABLE IF NOT EXISTS opening_window_pair_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    market_slug TEXT,
    open_ts TEXT NOT NULL,
    observed_ts TEXT NOT NULL,
    seconds_after_open REAL,
    yes_best_ask REAL,
    no_best_ask REAL,
    yes_liquidity REAL,
    no_liquidity REAL,
    pair_cost REAL,
    spread_adjusted_pair_cost REAL,
    btc_price REAL,
    strike REAL,
    distance_from_strike REAL,
    realized_vol_60s REAL,
    source TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(market_id, observed_ts, source)
);
CREATE INDEX IF NOT EXISTS idx_opening_pair_cost_market_time ON opening_window_pair_costs(market_id, seconds_after_open);
CREATE INDEX IF NOT EXISTS idx_opening_pair_cost_pair_cost ON opening_window_pair_costs(pair_cost);

CREATE TABLE IF NOT EXISTS wallet_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    market_id TEXT,
    condition_id TEXT,
    token_id TEXT,
    event_ts TEXT NOT NULL,
    side TEXT CHECK(side IN ('YES','NO','UNKNOWN')) DEFAULT 'UNKNOWN',
    action TEXT CHECK(action IN ('buy','sell','add','reduce','exit','unknown')) DEFAULT 'unknown',
    price REAL,
    size REAL,
    notional REAL,
    aggressor_side TEXT,
    tx_hash TEXT,
    trade_id TEXT,
    source TEXT NOT NULL,
    seconds_to_close INTEGER,
    market_slug TEXT,
    market_title TEXT,
    outcome TEXT,
    raw_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(wallet_address, trade_id, tx_hash, token_id, event_ts)
);
CREATE INDEX IF NOT EXISTS idx_wallet_events_wallet_ts ON wallet_events(wallet_address, event_ts);
CREATE INDEX IF NOT EXISTS idx_wallet_events_market_side_ts ON wallet_events(market_id, token_id, side, event_ts);
CREATE INDEX IF NOT EXISTS idx_wallet_events_condition_ts ON wallet_events(condition_id, event_ts);

CREATE TABLE IF NOT EXISTS wallet_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    market_id TEXT,
    condition_id TEXT,
    token_id TEXT,
    side TEXT,
    position_size REAL,
    avg_price REAL,
    realized_pnl REAL,
    unrealized_pnl REAL,
    snapshot_ts TEXT NOT NULL,
    source TEXT,
    raw_json TEXT,
    UNIQUE(wallet_address, token_id, snapshot_ts)
);

CREATE TABLE IF NOT EXISTS enriched_wallet_events (
    event_id INTEGER PRIMARY KEY,
    wallet_address TEXT NOT NULL,
    market_id TEXT,
    condition_id TEXT,
    token_id TEXT,
    event_ts TEXT NOT NULL,
    btc_price REAL,
    strike REAL,
    distance_from_strike REAL,
    distance_bps REAL,
    slope_15s REAL,
    slope_60s REAL,
    slope_180s REAL,
    realized_vol_60s REAL,
    realized_vol_180s REAL,
    atr_proxy REAL,
    orderbook_spread REAL,
    top_of_book_liquidity REAL,
    imbalance REAL,
    yes_mid REAL,
    no_mid REAL,
    time_bucket TEXT,
    enrichment_version TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(event_id) REFERENCES wallet_events(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_enriched_wallet_events_ts ON enriched_wallet_events(event_ts);

CREATE TABLE IF NOT EXISTS wallet_alpha (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    asof_ts TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    avg_edge_15s REAL,
    avg_edge_30s REAL,
    avg_edge_60s REAL,
    avg_edge_180s REAL,
    win_rate_15s REAL,
    win_rate_30s REAL,
    win_rate_60s REAL,
    win_rate_180s REAL,
    max_favorable_excursion REAL,
    max_adverse_excursion REAL,
    expiry_pnl REAL,
    expiry_win_rate REAL,
    sharpe_like_score REAL,
    consistency_by_regime TEXT,
    likely_market_maker INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    UNIQUE(wallet_address, asof_ts)
);
CREATE INDEX IF NOT EXISTS idx_wallet_alpha_wallet_asof ON wallet_alpha(wallet_address, asof_ts);

CREATE TABLE IF NOT EXISTS convergence_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_start_ts TEXT NOT NULL,
    cluster_end_ts TEXT NOT NULL,
    window_seconds INTEGER NOT NULL,
    market_id TEXT,
    condition_id TEXT,
    token_id TEXT,
    side TEXT,
    wallet_count INTEGER NOT NULL,
    wallet_addresses TEXT NOT NULL,
    consensus_score REAL NOT NULL,
    total_notional REAL,
    avg_wallet_alpha REAL,
    leader_wallet TEXT,
    follower_lags_seconds TEXT,
    directional_agreement REAL,
    market_regime TEXT,
    forward_markout_15s REAL,
    forward_markout_30s REAL,
    forward_markout_60s REAL,
    forward_markout_180s REAL,
    raw_event_ids TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_convergence_clusters_ts ON convergence_clusters(cluster_start_ts, cluster_end_ts);
CREATE INDEX IF NOT EXISTS idx_convergence_clusters_market ON convergence_clusters(market_id, token_id, side);

CREATE TABLE IF NOT EXISTS leader_follower_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    leader_wallet TEXT NOT NULL,
    follower_wallet TEXT NOT NULL,
    market_scope TEXT DEFAULT 'all',
    pair_count INTEGER NOT NULL,
    median_lag_seconds REAL,
    avg_favorable_move_after_leader REAL,
    influence_score REAL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(leader_wallet, follower_wallet, market_scope)
);

CREATE TABLE IF NOT EXISTS signal_replay_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    split_name TEXT NOT NULL,
    split_start_ts TEXT,
    split_end_ts TEXT,
    strategy_name TEXT NOT NULL,
    params_json TEXT NOT NULL,
    signal_count INTEGER NOT NULL,
    simulated_entry_mode TEXT,
    conservative_slippage_bps REAL,
    gross_pnl REAL,
    net_pnl REAL,
    avg_edge_cents REAL,
    win_rate REAL,
    max_drawdown REAL,
    sharpe_like_score REAL,
    random_baseline_pnl REAL,
    momentum_baseline_pnl REAL,
    artifact_path TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_signal_replay_run ON signal_replay_results(run_id, split_name);

CREATE TABLE IF NOT EXISTS raw_api_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    request_params TEXT,
    response_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);
