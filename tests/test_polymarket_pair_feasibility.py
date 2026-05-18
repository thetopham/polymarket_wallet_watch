from __future__ import annotations

import json
import sqlite3

from polymarket_wallet_watch.polymarket_pair_feasibility import format_pair_feasibility_report, scan_pair_feasibility


def _conn(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "poly-feed.sqlite3")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE realtime_snapshots_1s(
            ts TEXT PRIMARY KEY,
            market_slug TEXT,
            market_ticker TEXT,
            market_open_time TEXT,
            market_close_time TEXT,
            seconds_to_close REAL,
            btc_price REAL,
            yes_bid REAL,
            yes_ask REAL,
            no_bid REAL,
            no_ask REAL,
            yes_orderbook_json TEXT,
            no_orderbook_json TEXT,
            raw_json TEXT
        )
        """
    )
    return conn


def _insert(conn, *, ts, slug="btc-updown-15m-1779130800", seconds_to_close=800, yes_bid=0.40, yes_ask=0.43, no_bid=0.52, no_ask=0.55, yes_ask_depth=300, no_ask_depth=400):
    yes_book = {"bids": [{"price": str(yes_bid), "size": "25"}], "asks": [{"price": str(yes_ask), "size": str(yes_ask_depth)}]}
    no_book = {"bids": [{"price": str(no_bid), "size": "40"}], "asks": [{"price": str(no_ask), "size": str(no_ask_depth)}]}
    conn.execute(
        """
        INSERT INTO realtime_snapshots_1s(ts, market_slug, market_ticker, market_open_time, market_close_time, seconds_to_close,
                                          btc_price, yes_bid, yes_ask, no_bid, no_ask, yes_orderbook_json, no_orderbook_json, raw_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (ts, slug, slug, "2026-05-18T19:00:00+00:00", "2026-05-18T19:15:00+00:00", seconds_to_close, 76500, yes_bid, yes_ask, no_bid, no_ask, json.dumps(yes_book), json.dumps(no_book), "{}"),
    )
    conn.commit()


def test_scan_polymarket_pair_feasibility_finds_best_pair_cost_and_sizing(tmp_path):
    conn = _conn(tmp_path)
    _insert(conn, ts="2026-05-18T19:00:05+00:00", seconds_to_close=895, yes_ask=0.47, no_ask=0.48, yes_ask_depth=300, no_ask_depth=300)
    _insert(conn, ts="2026-05-18T19:04:00+00:00", seconds_to_close=660, yes_ask=0.44, no_ask=0.46, yes_ask_depth=250, no_ask_depth=180)
    _insert(conn, ts="2026-05-18T19:14:10+00:00", seconds_to_close=50, yes_ask=0.49, no_ask=0.50, yes_ask_depth=60, no_ask_depth=70)

    result = scan_pair_feasibility(conn, target_pair_cost=0.95, sizing_checks_usd=[50, 200, 1000])

    assert result["venue"] == "polymarket"
    assert result["contract_count"] == 1
    contract = result["contracts"][0]
    assert contract["market_key"] == "btc-updown-15m-1779130800"
    assert contract["best_pair_cost"] == 0.90
    assert contract["best_pair_cost_ts"] == "2026-05-18T19:04:00+00:00"
    assert contract["best_pair_cost_phase"] == "mid"
    assert contract["best_visible_pair_depth"] == 180.0
    assert contract["sizing_feasible"] == {"50": True, "200": False, "1000": False}
    assert contract["opportunity_count"] == 2


def test_format_polymarket_pair_feasibility_report(tmp_path):
    conn = _conn(tmp_path)
    _insert(conn, ts="2026-05-18T19:00:05+00:00", yes_ask=0.45, no_ask=0.45)

    text = format_pair_feasibility_report(scan_pair_feasibility(conn, target_pair_cost=0.95))

    assert "Polymarket 1s Pair Feasibility" in text
    assert "READ-ONLY" in text
    assert "btc-updown-15m-1779130800" in text
    assert "best pair cost" in text.lower()
    assert "$50 feasible" in text
