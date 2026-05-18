from __future__ import annotations

import sqlite3

from polymarket_wallet_watch.adapter_polymarket_1s import connect_feed
from polymarket_wallet_watch.db import connect, initialize_schema
from polymarket_wallet_watch.report import insert_wallet_event
from polymarket_wallet_watch.wallet_probability_edge import (
    _apply_portfolio_timeline,
    _apply_portfolio_timeline_by_market,
    analyze_wallet_probability_edges,
    format_wallet_probability_edge_report,
)


def _wallet_conn(tmp_path) -> sqlite3.Connection:
    conn = connect(tmp_path / "wallet.sqlite3")
    initialize_schema(conn)
    return conn


def _feed_conn(path) -> sqlite3.Connection:
    conn = connect_feed(path)
    conn.execute(
        """
        CREATE TABLE realtime_snapshots_1s (
            ts TEXT,
            market_slug TEXT,
            market_ticker TEXT,
            condition_id TEXT,
            yes_token_id TEXT,
            no_token_id TEXT,
            market_open_time TEXT,
            market_close_time TEXT,
            seconds_to_close REAL,
            btc_price REAL,
            strike REAL,
            yes_bid REAL,
            yes_ask REAL,
            no_bid REAL,
            no_ask REAL,
            yes_orderbook_json TEXT,
            no_orderbook_json TEXT
        )
        """
    )
    return conn


def _insert_snapshot(conn: sqlite3.Connection, *, ts: str, btc: float, strike: float, yes_bid: float = 0.40, yes_ask: float = 0.42, no_bid: float = 0.58, no_ask: float = 0.60) -> None:
    conn.execute(
        """
        INSERT INTO realtime_snapshots_1s(ts, market_slug, market_ticker, condition_id, yes_token_id, no_token_id,
                                          market_open_time, market_close_time, seconds_to_close, btc_price, strike,
                                          yes_bid, yes_ask, no_bid, no_ask)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (ts, "btc-updown-15m-test", "btc-updown-15m-test", "cond", "yes", "no", "2026-05-18T18:30:00+00:00", "2026-05-18T18:45:00+00:00", 60, btc, strike, yes_bid, yes_ask, no_bid, no_ask),
    )
    conn.commit()


def test_wallet_probability_edge_classifies_positive_model_edge(tmp_path) -> None:
    wallet_conn = _wallet_conn(tmp_path)
    feed_path = tmp_path / "feed.sqlite3"
    feed_conn = _feed_conn(feed_path)
    for i, btc in enumerate([100, 101, 102, 103, 104]):
        _insert_snapshot(feed_conn, ts=f"2026-05-18T18:30:0{i}+00:00", btc=btc, strike=100)
    insert_wallet_event(
        wallet_conn,
        {
            "wallet_address": "0xabc",
            "market_slug": "btc-updown-15m-test",
            "market_id": "m1",
            "condition_id": "cond",
            "token_id": "yes",
            "event_ts": "2026-05-18T18:30:04+00:00",
            "side": "YES",
            "action": "buy",
            "price": 0.42,
            "size": 10,
            "notional": 4.2,
            "source": "test",
            "trade_id": "t1",
        },
    )

    summary = analyze_wallet_probability_edges(wallet_conn, feed_db=str(feed_path), wallet="0xabc", since="2026-05-18T00:00:00+00:00", edge_threshold=0.03)

    assert summary["event_count"] == 1
    assert summary["with_snapshot_count"] == 1
    row = summary["rows"][0]
    assert row["classification"] == "positive_model_edge"
    assert row["wallet_role"] == "core_directional"
    assert row["portfolio_edge"] is not None
    assert row["model_edge_vs_fill"] is not None
    assert row["model_side_probability"] > row["fill_price"]
    assert summary["markets_analyzed"] == 1
    assert summary["market_summaries"][0]["market_slug"] == "btc-updown-15m-test"
    persisted = wallet_conn.execute("SELECT COUNT(*) FROM wallet_probability_edge_events").fetchone()[0]
    assert persisted == 1


def test_wallet_probability_edge_report_includes_context(tmp_path) -> None:
    wallet_conn = _wallet_conn(tmp_path)
    feed_path = tmp_path / "feed.sqlite3"
    feed_conn = _feed_conn(feed_path)
    _insert_snapshot(feed_conn, ts="2026-05-18T18:30:00+00:00", btc=100, strike=100)
    insert_wallet_event(
        wallet_conn,
        {
            "wallet_address": "0xabc",
            "market_slug": "btc-updown-15m-test",
            "event_ts": "2026-05-18T18:30:00+00:00",
            "side": "NO",
            "action": "buy",
            "price": 0.59,
            "size": 5,
            "notional": 2.95,
            "source": "test",
            "trade_id": "t2",
        },
    )

    summary = analyze_wallet_probability_edges(wallet_conn, feed_db=str(feed_path), wallet="0xabc", since="2026-05-18T00:00:00+00:00", edge_threshold=0.03, persist=False)
    text = format_wallet_probability_edge_report(summary)

    assert "Wallet Probability Edge Report" in text
    assert "snapshot_matched=1" in text
    assert "edge_fill" in text
    assert "Aggregate markets" in text
    assert "Per-market portfolio summaries" in text
    assert "Role counts" in text
    assert "no private keys" in text.lower()


def test_portfolio_timeline_classifies_roles_and_combined_ev() -> None:
    rows = [
        {"side": "YES", "size": 10.0, "fill_price": 0.60, "model_edge_vs_fill": 0.20, "model_yes_probability": 0.80, "model_no_probability": 0.20},
        {"side": "NO", "size": 5.0, "fill_price": 0.10, "model_edge_vs_fill": -0.05, "model_yes_probability": 0.80, "model_no_probability": 0.20},
        {"side": "NO", "size": 10.0, "fill_price": 0.30, "model_edge_vs_fill": -0.10, "model_yes_probability": 0.70, "model_no_probability": 0.30},
        {"side": "YES", "size": 2.0, "fill_price": 0.90, "model_edge_vs_fill": -0.30, "model_yes_probability": 0.60, "model_no_probability": 0.40},
    ]

    _apply_portfolio_timeline(rows, edge_threshold=0.03, hedge_max_price=0.20)

    assert rows[0]["wallet_role"] == "core_directional"
    assert rows[1]["wallet_role"] == "cheap_hedge"
    assert rows[2]["wallet_role"] == "inventory_repair"
    assert rows[3]["wallet_role"] == "bad_fill_or_noise"
    assert rows[-1]["yes_qty_after"] == 12.0
    assert rows[-1]["no_qty_after"] == 15.0
    assert rows[-1]["portfolio_cost"] == 11.3
    assert rows[-1]["portfolio_model_value"] == 13.2
    assert rows[-1]["portfolio_edge"] == 1.9


def test_market_portfolios_reset_independently() -> None:
    rows = [
        {"market_slug": "market-a", "event_ts": "t1", "side": "YES", "size": 10.0, "fill_price": 0.40, "model_edge_vs_fill": 0.20, "model_yes_probability": 0.70, "model_no_probability": 0.30, "classification": "positive_model_edge", "nearest_snapshot_ts": "s1"},
        {"market_slug": "market-b", "event_ts": "t2", "side": "NO", "size": 5.0, "fill_price": 0.20, "model_edge_vs_fill": 0.10, "model_yes_probability": 0.60, "model_no_probability": 0.40, "classification": "positive_model_edge", "nearest_snapshot_ts": "s2"},
    ]

    summaries = _apply_portfolio_timeline_by_market(rows, edge_threshold=0.03, hedge_max_price=0.20)

    assert summaries["market-a"]["yes_qty"] == 10.0
    assert summaries["market-a"]["no_qty"] == 0.0
    assert summaries["market-a"]["final_portfolio_cost"] == 4.0
    assert summaries["market-a"]["final_portfolio_model_value"] == 7.0
    assert summaries["market-a"]["final_portfolio_edge"] == 3.0
    assert summaries["market-b"]["yes_qty"] == 0.0
    assert summaries["market-b"]["no_qty"] == 5.0
    assert summaries["market-b"]["final_portfolio_cost"] == 1.0
    assert summaries["market-b"]["final_portfolio_model_value"] == 2.0
    assert summaries["market-b"]["final_portfolio_edge"] == 1.0
    assert rows[0]["yes_qty_after"] == 10.0
    assert rows[1]["yes_qty_after"] == 0.0
