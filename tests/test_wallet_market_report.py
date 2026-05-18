from polymarket_wallet_watch.db import connect, initialize_schema
from polymarket_wallet_watch.report import insert_wallet_event
from polymarket_wallet_watch.wallet_market_report import build_wallet_market_inventory, format_wallet_market_report


def _conn(tmp_path):
    conn = connect(tmp_path / "watch.sqlite3")
    initialize_schema(conn)
    return conn


def test_wallet_market_inventory_calculates_pair_cost_profit_and_inventory_swings(tmp_path):
    conn = _conn(tmp_path)
    rows = [
        {"wallet_address": "0xabc", "market_id": "m1", "condition_id": "c1", "token_id": "yes", "event_ts": "2026-05-18T18:00:05+00:00", "side": "YES", "action": "buy", "price": 0.54, "size": 100, "notional": 54.0, "source": "test", "market_slug": "btc-updown-5m-1779129000", "trade_id": "f1"},
        {"wallet_address": "0xabc", "market_id": "m1", "condition_id": "c1", "token_id": "no", "event_ts": "2026-05-18T18:00:10+00:00", "side": "NO", "action": "buy", "price": 0.35, "size": 80, "notional": 28.0, "source": "test", "market_slug": "btc-updown-5m-1779129000", "trade_id": "f2"},
        {"wallet_address": "0xabc", "market_id": "m1", "condition_id": "c1", "token_id": "yes", "event_ts": "2026-05-18T18:02:00+00:00", "side": "YES", "action": "sell", "price": 0.62, "size": 30, "notional": 18.6, "source": "test", "market_slug": "btc-updown-5m-1779129000", "trade_id": "f3"},
        {"wallet_address": "0xabc", "market_id": "m1", "condition_id": "c1", "token_id": "no", "event_ts": "2026-05-18T18:03:00+00:00", "side": "NO", "action": "buy", "price": 0.37, "size": 20, "notional": 7.4, "source": "test", "market_slug": "btc-updown-5m-1779129000", "trade_id": "f4"},
        {"wallet_address": "0xdef", "market_id": "m2", "condition_id": "c2", "token_id": "yes2", "event_ts": "2026-05-18T18:00:00+00:00", "side": "YES", "action": "buy", "price": 0.1, "size": 999, "notional": 99.9, "source": "test", "market_slug": "other-market", "trade_id": "other"},
    ]
    for row in rows:
        insert_wallet_event(conn, row)

    report = build_wallet_market_inventory(conn, wallet="0xAbC", market_slug="btc-updown-5m-1779129000")

    assert report["event_count"] == 4
    assert report["yes_buys"]["qty"] == 100.0
    assert report["yes_buys"]["avg_price"] == 0.54
    assert report["yes_buys"]["notional"] == 54.0
    assert report["no_buys"]["qty"] == 100.0
    assert report["no_buys"]["avg_price"] == 0.354
    assert report["no_buys"]["notional"] == 35.4
    assert report["matched_pair_qty"] == 100.0
    assert report["matched_pair_cost"] == 0.894
    assert report["locked_profit_if_held"] == 10.6
    assert report["created_pair_below_one"] is True
    assert report["unpaired_yes_qty"] == 0.0
    assert report["unpaired_no_qty"] == 0.0
    assert report["remaining_exposure"] == "balanced_pair"
    assert report["max_inventory_swing"]["YES"] == 100.0
    assert report["max_inventory_swing"]["NO"] == 100.0
    assert report["first_entry_ts"] == "2026-05-18T18:00:05+00:00"
    assert report["last_entry_ts"] == "2026-05-18T18:03:00+00:00"
    assert [event["trade_id"] for event in report["event_timeline"]] == ["f1", "f2", "f3", "f4"]


def test_wallet_market_report_filters_since_asset_and_interval(tmp_path):
    conn = _conn(tmp_path)
    rows = [
        {"wallet_address": "0xabc", "market_id": "m1", "event_ts": "2026-05-18T18:00:00+00:00", "side": "YES", "action": "buy", "price": 0.55, "size": 10, "notional": 5.5, "source": "test", "market_slug": "btc-updown-5m-1779129000", "trade_id": "keep"},
        {"wallet_address": "0xabc", "market_id": "m1", "event_ts": "2026-05-17T18:00:00+00:00", "side": "NO", "action": "buy", "price": 0.40, "size": 10, "notional": 4.0, "source": "test", "market_slug": "btc-updown-5m-1779129000", "trade_id": "old"},
        {"wallet_address": "0xabc", "market_id": "m2", "event_ts": "2026-05-18T18:00:00+00:00", "side": "NO", "action": "buy", "price": 0.44, "size": 10, "notional": 4.4, "source": "test", "market_slug": "eth-updown-5m-1779129000", "trade_id": "wrong-asset"},
        {"wallet_address": "0xabc", "market_id": "m3", "event_ts": "2026-05-18T18:00:00+00:00", "side": "NO", "action": "buy", "price": 0.44, "size": 10, "notional": 4.4, "source": "test", "market_slug": "btc-updown-15m-1779129000", "trade_id": "wrong-interval"},
    ]
    for row in rows:
        insert_wallet_event(conn, row)

    report = build_wallet_market_inventory(
        conn,
        wallet="0xabc",
        market_slug="btc-updown-5m-1779129000",
        since="2026-05-18T00:00:00+00:00",
        asset="btc",
        interval="5m",
    )
    text = format_wallet_market_report(report)

    assert report["event_count"] == 1
    assert report["event_timeline"][0]["trade_id"] == "keep"
    assert "YES+NO pair below 1.00: NO" in text
    assert "Remaining exposure: directional_yes" in text
    assert "keep" in text
    assert "old" not in text
