from __future__ import annotations

import json
import sqlite3

from polymarket_wallet_watch.adapter_polymarket_1s import load_nearest_snapshot, normalize_snapshot_row, parse_orderbook_depth


def _conn(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "poly-feed.sqlite3")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE realtime_snapshots_1s(
            ts TEXT PRIMARY KEY,
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
            no_orderbook_json TEXT,
            raw_book_json TEXT,
            raw_json TEXT
        )
        """
    )
    return conn


def _insert(conn, ts: str, yes_bid=0.40, yes_ask=0.43, no_bid=0.52, no_ask=0.55) -> None:
    yes_book = {"bids": [{"price": str(yes_bid), "size": "25"}], "asks": [{"price": str(yes_ask), "size": "30"}]}
    no_book = {"bids": [{"price": str(no_bid), "size": "40"}], "asks": [{"price": str(no_ask), "size": "50"}]}
    conn.execute(
        """
        INSERT INTO realtime_snapshots_1s(ts, market_slug, market_ticker, condition_id, yes_token_id, no_token_id,
                                          market_open_time, market_close_time, seconds_to_close, btc_price, strike,
                                          yes_bid, yes_ask, no_bid, no_ask, yes_orderbook_json, no_orderbook_json, raw_book_json, raw_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ts,
            "btc-updown-15m-1779130800",
            "btc-updown-15m-1779130800",
            "cond1",
            "yes-token",
            "no-token",
            "2026-05-18T19:00:00+00:00",
            "2026-05-18T19:15:00+00:00",
            840,
            76500,
            76100,
            yes_bid,
            yes_ask,
            no_bid,
            no_ask,
            json.dumps(yes_book),
            json.dumps(no_book),
            json.dumps({"yes": yes_book, "no": no_book}),
            json.dumps({"venue": "polymarket"}),
        ),
    )
    conn.commit()


def test_parse_polymarket_orderbook_depth_from_json() -> None:
    book = {"bids": [{"price": "0.41", "size": "12"}], "asks": [{"price": "0.44", "size": "34"}]}

    assert parse_orderbook_depth(book) == {"bid_depth": 12.0, "ask_depth": 34.0}


def test_load_nearest_polymarket_snapshot_normalizes_pair_metrics(tmp_path) -> None:
    conn = _conn(tmp_path)
    _insert(conn, "2026-05-18T19:00:00+00:00", yes_bid=0.40, yes_ask=0.43, no_bid=0.52, no_ask=0.55)
    _insert(conn, "2026-05-18T19:00:02+00:00", yes_bid=0.42, yes_ask=0.44, no_bid=0.50, no_ask=0.53)

    snap = load_nearest_snapshot(conn, "2026-05-18T19:00:01+00:00", market_key="btc-updown-15m-1779130800")

    assert snap is not None
    assert snap["venue"] == "polymarket"
    assert snap["market_key"] == "btc-updown-15m-1779130800"
    assert snap["yes_bid"] == 0.40
    assert snap["yes_ask"] == 0.43
    assert snap["no_bid"] == 0.52
    assert snap["no_ask"] == 0.55
    assert snap["yes_bid_depth"] == 25.0
    assert snap["yes_ask_depth"] == 30.0
    assert snap["no_bid_depth"] == 40.0
    assert snap["no_ask_depth"] == 50.0
    assert snap["spread"] == 0.06
    assert snap["pair_ask_cost"] == 0.98
    assert snap["pair_bid_credit"] == 0.92


def test_normalize_snapshot_row_uses_raw_book_fallback(tmp_path) -> None:
    conn = _conn(tmp_path)
    _insert(conn, "2026-05-18T19:00:00+00:00")
    row = conn.execute("SELECT * FROM realtime_snapshots_1s LIMIT 1").fetchone()

    snap = normalize_snapshot_row(row)

    assert snap["yes_ask_depth"] == 30.0
    assert snap["no_ask_depth"] == 50.0
    assert snap["pair_ask_cost"] == 0.98
