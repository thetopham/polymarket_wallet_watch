import sqlite3

from polymarket_wallet_watch.btc15m_focus_deconstruction import build_btc15m_deconstruction, format_deconstruction_report
from polymarket_wallet_watch.db import connect, initialize_schema
from polymarket_wallet_watch.report import insert_wallet_event


def _main_conn(tmp_path):
    conn = connect(tmp_path / "watch.sqlite3")
    initialize_schema(conn)
    return conn


def _feed_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE realtime_snapshots_1s(
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


def test_btc15m_focus_deconstruction_tracks_running_pair_and_orderbook(tmp_path):
    conn = _main_conn(tmp_path)
    feed = _feed_conn()
    wallet = "0xce25e214d5cfe4f459cf67f08df581885aae7fdc"
    slug = "btc-updown-15m-1779143400"
    rows = [
        {"wallet_address": wallet, "event_ts": "2026-05-18T22:30:51+00:00", "market_slug": slug, "side": "NO", "action": "buy", "price": 0.49, "size": 10, "notional": 4.9, "source": "test", "trade_id": "n1"},
        {"wallet_address": wallet, "event_ts": "2026-05-18T22:31:24+00:00", "market_slug": slug, "side": "YES", "action": "buy", "price": 0.42, "size": 10, "notional": 4.2, "source": "test", "trade_id": "y1"},
        {"wallet_address": wallet, "event_ts": "2026-05-18T22:31:42+00:00", "market_slug": slug, "side": "YES", "action": "buy", "price": 0.46, "size": 10, "notional": 4.6, "source": "test", "trade_id": "y2"},
    ]
    for row in rows:
        insert_wallet_event(conn, row)
    feed_rows = [
        ("2026-05-18T22:30:51+00:00", slug, 0.50, 0.51, 0.49, 0.50),
        ("2026-05-18T22:31:24+00:00", slug, 0.42, 0.43, 0.57, 0.58),
        ("2026-05-18T22:31:42+00:00", slug, 0.46, 0.47, 0.53, 0.54),
    ]
    for ts, market_slug, yes_bid, yes_ask, no_bid, no_ask in feed_rows:
        feed.execute(
            """
            INSERT INTO realtime_snapshots_1s(ts, market_slug, yes_bid, yes_ask, no_bid, no_ask, btc_price, strike,
                                               yes_orderbook_json, no_orderbook_json)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (ts, market_slug, yes_bid, yes_ask, no_bid, no_ask, 77000.0, 77100.0,
             '{"bids":[{"price":"0.42","size":"50"}],"asks":[{"price":"0.43","size":"40"}]}',
             '{"bids":[{"price":"0.49","size":"60"}],"asks":[{"price":"0.50","size":"30"}]}'),
        )
    feed.commit()

    report = build_btc15m_deconstruction(conn, feed, wallet=wallet)

    assert report["market_count"] == 1
    assert report["fill_count"] == 3
    assert report["sub_100_pair_markets"] == 1
    market = report["market_summaries"][0]
    assert market["matched_pair_cost"] == 0.93
    assert market["matched_edge_if_held"] == 0.7
    assert market["near_bid_fills"] == 3
    assert "mostly_passive_bid_fills" in market["strategy_tags"]
    fills = report["fill_contexts"]
    assert fills[1]["matched_pair_cost_after"] == 0.91
    assert fills[1]["fill_role"] in {"pair_repair_under_095", "add_under_095"}
    assert fills[1]["orderbook_class"] == "near_bid_passive"
    text = format_deconstruction_report(report, max_markets=1, max_fills=3)
    assert "BTC 15m Focus Wallet Strategy Deconstruction" in text
    assert "sub_1.00_pair_markets=1" in text


def test_btc15m_focus_deconstruction_ignores_non_btc15m(tmp_path):
    conn = _main_conn(tmp_path)
    wallet = "0xce25e214d5cfe4f459cf67f08df581885aae7fdc"
    insert_wallet_event(conn, {"wallet_address": wallet, "event_ts": "2026-05-18T22:30:00+00:00", "market_slug": "btc-updown-5m-1779143400", "side": "YES", "action": "buy", "price": 0.5, "size": 10, "notional": 5, "source": "test", "trade_id": "skip"})
    report = build_btc15m_deconstruction(conn, None, wallet=wallet)
    assert report["market_count"] == 0
    assert report["fill_count"] == 0
