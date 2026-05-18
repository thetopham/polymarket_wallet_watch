from polymarket_wallet_watch.db import connect, initialize_schema
from polymarket_wallet_watch.report import format_last_events_report, insert_wallet_event


def test_report_prints_last_normalized_wallet_events(tmp_path):
    conn = connect(tmp_path / "watch.sqlite3")
    initialize_schema(conn)
    insert_wallet_event(
        conn,
        {
            "wallet_address": "0xabc",
            "market_id": "m1",
            "condition_id": "c1",
            "token_id": "t1",
            "event_ts": "2026-05-18T12:00:00+00:00",
            "side": "YES",
            "action": "buy",
            "price": 0.61,
            "size": 10,
            "notional": 6.1,
            "source": "test",
            "seconds_to_close": 90,
            "market_slug": "btc-updown",
            "market_title": "BTC Up/Down",
            "outcome": "Yes",
        },
    )

    text = format_last_events_report(conn, limit=100)

    assert "Polymarket Wallet Watch" in text
    assert "READ-ONLY RESEARCH" in text
    assert "0xabc" in text
    assert "btc-updown" in text
    assert "YES buy" in text
