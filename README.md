# Polymarket Wallet Watch

Read-only wallet-convergence research for Polymarket BTC-style prediction markets.

This project does not live trade. It stores no private keys, submits no orders, and only ingests public/readonly data for research and later paper-trading evaluation.

## Thesis

A single profitable wallet can be noisy, hedged, market-making, or lucky. Multiple historically profitable wallets converging on the same market/outcome within a short window may be a stronger signal. This system tracks selected wallets, enriches their events with market state, scores post-trade alpha, detects consensus clusters, and replays signals before any future paper-trading use.

## Structure

polymarket_wallet_watch/
  README.md
  PRD.md
  config.example.yaml
  pyproject.toml
  sql/schema.sql
  src/polymarket_wallet_watch/
    ingest_markets.py
    ingest_wallets.py
    ingest_orderbooks.py
    enrich_market_state.py
    detect_convergence.py
    score_wallets.py
    leader_follower.py
    replay_signals.py
    study_opening_window.py
    report.py
  tests/

## Setup

Use a venv, not system Python:

    cd /home/matt/polymarket_wallet_watch
    python3 -m venv .venv
    .venv/bin/python -m pip install -e '.[dev]'
    cp config.example.yaml config.yaml

Edit config.yaml and enable real public wallet addresses under wallets.

Secrets, if needed for read-only RPC/indexer usage, belong in .env only. Never commit .env.

## First milestone commands

Initialize the SQLite schema:

    .venv/bin/python - <<'PY'
    from polymarket_wallet_watch.db import connect, initialize_schema
    c = connect('data/polymarket_wallet_watch.sqlite3')
    initialize_schema(c)
    print('ok')
    PY

Ingest market metadata:

    .venv/bin/python -m polymarket_wallet_watch.ingest_markets --config config.yaml

Ingest public wallet events for enabled wallets:

    .venv/bin/python -m polymarket_wallet_watch.ingest_wallets --config config.yaml

Print last 100 normalized wallet events:

    .venv/bin/python -m polymarket_wallet_watch.report --config config.yaml --since 24h

## Later milestone commands

Fetch public orderbook snapshots for known token IDs:

    .venv/bin/python -m polymarket_wallet_watch.ingest_orderbooks --config config.yaml --token-id TOKEN_ID

Enrich wallet events from the local BTC/Kalshi SQLite feed:

    .venv/bin/python -m polymarket_wallet_watch.enrich_market_state --config config.yaml --limit 1000

Score wallets:

    .venv/bin/python -m polymarket_wallet_watch.score_wallets --config config.yaml

Detect convergence clusters:

    .venv/bin/python -m polymarket_wallet_watch.detect_convergence --config config.yaml --window 30

Replay signals with chronological split:

    .venv/bin/python -m polymarket_wallet_watch.replay_signals --config config.yaml --split-date YYYY-MM-DD

Export leader/follower table:

    .venv/bin/python -m polymarket_wallet_watch.leader_follower --config config.yaml --output leader_follower_edges.csv

## Opening Window Pair-Cost Study

Current priority: before broad convergence work, test whether BTC 15m markets repeatedly offer cheap opening-window pair inventory such as:

    YES 47 + NO 43 = 90

Goal: measure whether the first 15-120 seconds after contract open offer systematically better dual-side inventory opportunities than later windows.

Metrics:
- best_yes_fill_open
- best_no_fill_open
- pair_cost_open = yes + no
- min_pair_cost_full_contract
- time_of_min_pair_cost
- spread-adjusted pair cost
- available liquidity at pair cost
- later exit value before expiry
- expiry pnl if held

Questions:
- Is open actually the best time?
- Does open edge survive spread/slippage?
- Does liquidity support $50, $200, $1000+ sizing?
- Do high-alpha wallets enter both sides at open?
- Do they hold, trim, or flip before expiry?

Command:

    .venv/bin/python -m polymarket_wallet_watch.study_opening_window --config config.yaml --window-seconds 120

The command is bounded by default. For a 1 Hz two-minute capture, run with `--max-iterations 120 --poll-seconds 1` during an active open window. It only reads public CLOB books and writes SQLite rows to `opening_window_pair_costs`.

Best next concrete step:
1. Track 15m BTC markets at open.
2. Snapshot YES/NO books every 1s for first 2 minutes.
3. Compute pair_cost and liquidity.
4. Compare against wallet entries.

If open frequently gives `YES + NO < 0.95` after spread/slippage and enough visible depth, that becomes the base inventory research strategy. Wallet convergence becomes the confirmation layer.

## Safety boundary

Allowed:
- Public Gamma/Data/CLOB reads.
- Optional read-only Polygon RPC/indexer reads.
- Local SQLite writes for observations, enrichment, scores, clusters, and replay results.
- Reports and CSV exports.

Not allowed:
- Private keys.
- Signers.
- Authenticated trading clients.
- Live orders.
- Automatic trading.

## Testing

    .venv/bin/python -m pytest -q

Current tests cover schema creation, wallet event normalization, wallet scoring, convergence clustering, and report formatting.
