from polymarket_wallet_watch.db import connect, initialize_schema
from polymarket_wallet_watch.study_opening_window import (
    compute_pair_cost_row,
    format_opening_window_report,
    insert_pair_cost_row,
)


def test_schema_creates_opening_window_pair_costs(tmp_path):
    conn = connect(tmp_path / "watch.sqlite3")
    initialize_schema(conn)

    cols = {row[1] for row in conn.execute("pragma table_info(opening_window_pair_costs)")}

    assert {
        "market_id",
        "market_slug",
        "open_ts",
        "observed_ts",
        "seconds_after_open",
        "yes_best_ask",
        "no_best_ask",
        "yes_liquidity",
        "no_liquidity",
        "pair_cost",
        "spread_adjusted_pair_cost",
        "btc_price",
        "strike",
        "distance_from_strike",
        "realized_vol_60s",
        "source",
    }.issubset(cols)


def test_compute_pair_cost_row_from_yes_no_books():
    row = compute_pair_cost_row(
        market_id="m1",
        market_slug="btc-updown-15m-123",
        open_ts="2026-05-18T12:00:00+00:00",
        observed_ts="2026-05-18T12:00:15+00:00",
        yes_book={"asks": [{"price": "0.47", "size": "250"}], "bids": [{"price": "0.46", "size": "100"}]},
        no_book={"asks": [{"price": "0.43", "size": "180"}], "bids": [{"price": "0.42", "size": "90"}]},
        btc_price=104950.0,
        strike=105000.0,
        realized_vol_60s=0.0012,
        source="unit-test",
    )

    assert row["seconds_after_open"] == 15.0
    assert row["yes_best_ask"] == 0.47
    assert row["no_best_ask"] == 0.43
    assert row["pair_cost"] == 0.90
    assert row["spread_adjusted_pair_cost"] == 0.92
    assert row["yes_liquidity"] == 250.0
    assert row["no_liquidity"] == 180.0
    assert row["distance_from_strike"] == -50.0


def test_insert_pair_cost_row_and_report_highlights_min(tmp_path):
    conn = connect(tmp_path / "watch.sqlite3")
    initialize_schema(conn)
    insert_pair_cost_row(
        conn,
        {
            "market_id": "m1",
            "market_slug": "btc-updown",
            "open_ts": "2026-05-18T12:00:00+00:00",
            "observed_ts": "2026-05-18T12:00:15+00:00",
            "seconds_after_open": 15.0,
            "yes_best_ask": 0.47,
            "no_best_ask": 0.43,
            "yes_liquidity": 250.0,
            "no_liquidity": 180.0,
            "pair_cost": 0.90,
            "spread_adjusted_pair_cost": 0.92,
            "btc_price": 104950.0,
            "strike": 105000.0,
            "distance_from_strike": -50.0,
            "realized_vol_60s": 0.0012,
            "source": "unit-test",
        },
    )

    report = format_opening_window_report(conn, limit=10)

    assert "Opening Window Pair-Cost Study" in report
    assert "m1" in report
    assert "pair=0.9000" in report
    assert "spread_adj=0.9200" in report
