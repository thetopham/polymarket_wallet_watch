from polymarket_wallet_watch.db import connect, initialize_schema
from polymarket_wallet_watch.follow_wallet import format_wallet_follow_report, normalize_wallet_address
from polymarket_wallet_watch.report import insert_wallet_event

FOCUS = "0xce25e214d5cfe4f459cf67f08df581885aae7fdc"


def test_normalize_wallet_address_lowercases_and_validates():
    assert normalize_wallet_address(FOCUS.upper()) == FOCUS


def test_follow_wallet_report_filters_single_wallet(tmp_path):
    conn = connect(tmp_path / "watch.sqlite3")
    initialize_schema(conn)
    insert_wallet_event(conn, {
        "wallet_address": FOCUS,
        "market_id": "m1",
        "condition_id": "c1",
        "token_id": "yes1",
        "event_ts": "2026-05-18T12:00:00+00:00",
        "side": "YES",
        "action": "buy",
        "price": 0.47,
        "size": 100,
        "notional": 47,
        "source": "test",
        "seconds_to_close": 850,
        "market_slug": "btc-updown-1",
        "market_title": "BTC Up/Down",
        "outcome": "Yes",
    })
    insert_wallet_event(conn, {
        "wallet_address": "0xeebde7a0e019a63e6b476eb425505b7b3e6eba30",
        "market_id": "m2",
        "condition_id": "c2",
        "token_id": "no2",
        "event_ts": "2026-05-18T12:01:00+00:00",
        "side": "NO",
        "action": "buy",
        "price": 0.55,
        "size": 50,
        "notional": 27.5,
        "source": "test",
        "seconds_to_close": 780,
        "market_slug": "other-market",
        "market_title": "Other",
        "outcome": "No",
    })

    report = format_wallet_follow_report(conn, FOCUS, limit=10)

    assert "Individual Wallet Follow" in report
    assert FOCUS in report
    assert "btc-updown-1" in report
    assert "YES buy" in report
    assert "other-market" not in report


def test_follow_wallet_report_mentions_no_orders(tmp_path):
    conn = connect(tmp_path / "watch.sqlite3")
    initialize_schema(conn)

    report = format_wallet_follow_report(conn, FOCUS, limit=10)

    assert "READ-ONLY" in report
    assert "no private keys, no orders" in report
