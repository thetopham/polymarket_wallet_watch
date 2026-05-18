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
    follow_wallet.py
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

Follow the focus wallet individually:

    .venv/bin/python -m polymarket_wallet_watch.follow_wallet --config config.yaml

Follow any one public wallet explicitly:

    .venv/bin/python -m polymarket_wallet_watch.follow_wallet \
      --config config.yaml \
      --wallet 0xce25e214d5cfe4f459cf67f08df581885aae7fdc \
      --since 24h

The focus wallet is tracked individually first. Two-wallet convergence is then used as confirmation, not as a replacement for understanding the wallet on its own.

## Later milestone commands

Fetch public orderbook snapshots for known token IDs:

    .venv/bin/python -m polymarket_wallet_watch.ingest_orderbooks --config config.yaml --token-id TOKEN_ID

Enrich wallet events from the local BTC/Kalshi SQLite feed:

    .venv/bin/python -m polymarket_wallet_watch.enrich_market_state --config config.yaml --limit 1000

Score wallets:

    .venv/bin/python -m polymarket_wallet_watch.score_wallets --config config.yaml

Detect two-wallet convergence clusters:

    .venv/bin/python -m polymarket_wallet_watch.detect_convergence --config config.yaml --window 30 --min-wallets 2

Replay signals with chronological split:

    .venv/bin/python -m polymarket_wallet_watch.replay_signals --config config.yaml --split-date YYYY-MM-DD

Export leader/follower table:

    .venv/bin/python -m polymarket_wallet_watch.leader_follower --config config.yaml --output leader_follower_edges.csv

Serve the read-only local dashboard:

    .venv/bin/python -m polymarket_wallet_watch.dashboard --config config.yaml --host 127.0.0.1 --port 8793

The dashboard is API-neutral: it reads local SQLite only and does not call Polymarket, Polygon, or broker APIs. Refresh data with the ingest/enrichment commands separately.

## Full Contract Pair-Cost Study

Current priority: before broad convergence work, test whether BTC 15m markets repeatedly offer cheap full-contract pair inventory such as:

    YES 47 + NO 43 = 90

Goal: measure pair-cost opportunities across the full 15-minute contract window and identify whether the minimum usable YES+NO cost occurs at open, mid-contract, or near close.

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
- Is open, mid-contract, or near-close actually the best time?
- Does pair-cost edge survive spread/slippage?
- Does visible liquidity support $50, $200, $1000+ sizing throughout the contract?
- Do high-alpha wallets enter both sides during the contract window?
- Do they hold, trim, or flip before expiry?

Command:

    .venv/bin/python -m polymarket_wallet_watch.study_opening_window --config config.yaml --window-seconds 900

The command is bounded by default. For a 1 Hz full-contract capture, run with `--max-iterations 900 --poll-seconds 1` during an active 15-minute contract window. It only reads public CLOB books and writes SQLite rows to `opening_window_pair_costs`.

Best next concrete step:
1. Track 15m BTC markets from open through close.
2. Snapshot YES/NO books every 1s for full 15 minutes.
3. Compute pair_cost and liquidity.
4. Compare against wallet entries.

If the full contract frequently gives `YES + NO < 0.95` after spread/slippage and enough visible depth, that becomes the base inventory research strategy. Wallet convergence becomes the confirmation layer.

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
