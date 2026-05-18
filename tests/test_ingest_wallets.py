from datetime import datetime, timezone

from polymarket_wallet_watch.ingest_wallets import normalize_wallet_trade


def test_normalize_wallet_trade_derives_side_action_and_notional():
    raw = {
        "id": "trade-1",
        "transactionHash": "0xabc",
        "timestamp": "2026-05-18T12:00:00Z",
        "conditionId": "cond-1",
        "asset": "token-yes",
        "side": "BUY",
        "outcome": "Yes",
        "price": "0.63",
        "size": "12.5",
        "market": {
            "id": "m-1",
            "slug": "btc-updown-15m-123",
            "question": "Bitcoin Up or Down?",
            "endDate": "2026-05-18T12:15:00Z",
        },
    }

    event = normalize_wallet_trade("0xWallet", raw)

    assert event["wallet_address"] == "0xwallet"
    assert event["trade_id"] == "trade-1"
    assert event["tx_hash"] == "0xabc"
    assert event["market_id"] == "m-1"
    assert event["condition_id"] == "cond-1"
    assert event["token_id"] == "token-yes"
    assert event["side"] == "YES"
    assert event["action"] == "buy"
    assert event["price"] == 0.63
    assert event["size"] == 12.5
    assert event["notional"] == 7.875
    assert event["seconds_to_close"] == 900
    assert event["market_slug"] == "btc-updown-15m-123"
    assert event["market_title"] == "Bitcoin Up or Down?"
    assert event["source"] == "polymarket_clob"


def test_normalize_wallet_trade_handles_epoch_timestamp_and_sells():
    raw = {
        "id": "2",
        "timestamp": 1779105600,
        "conditionId": "cond-2",
        "token_id": "token-no",
        "side": "SELL",
        "outcome": "No",
        "price": 0.41,
        "size": 20,
        "market_slug": "btc-test",
        "title": "BTC test",
    }

    event = normalize_wallet_trade("0xABC", raw)

    assert event["event_ts"] == datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc).isoformat()
    assert event["side"] == "NO"
    assert event["action"] == "sell"
    assert event["market_slug"] == "btc-test"
