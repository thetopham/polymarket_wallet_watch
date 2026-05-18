import sqlite3
from pathlib import Path

from polymarket_wallet_watch.db import connect, initialize_schema


def test_schema_creates_required_tables(tmp_path):
    db_path = tmp_path / "watch.sqlite3"
    conn = connect(db_path)
    initialize_schema(conn)

    tables = {
        row[0]
        for row in conn.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
        )
    }

    assert {
        "wallets",
        "markets",
        "market_snapshots",
        "wallet_events",
        "wallet_positions",
        "enriched_wallet_events",
        "wallet_alpha",
        "convergence_clusters",
        "leader_follower_edges",
        "signal_replay_results",
        "raw_api_responses",
    }.issubset(tables)


def test_wallet_events_has_research_fields(tmp_path):
    conn = connect(tmp_path / "watch.sqlite3")
    initialize_schema(conn)

    cols = {row[1] for row in conn.execute("pragma table_info(wallet_events)")}

    assert {
        "wallet_address",
        "market_id",
        "condition_id",
        "token_id",
        "event_ts",
        "side",
        "action",
        "price",
        "size",
        "notional",
        "aggressor_side",
        "tx_hash",
        "trade_id",
        "source",
        "seconds_to_close",
        "market_slug",
        "market_title",
        "outcome",
    }.issubset(cols)


def test_schema_file_is_loadable_directly():
    schema_path = Path(__file__).resolve().parents[1] / "sql" / "schema.sql"
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema_path.read_text())
    assert conn.execute("select count(*) from sqlite_master where type='table'").fetchone()[0] >= 10
