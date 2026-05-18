import json

from polymarket_wallet_watch.db import connect, initialize_schema
from polymarket_wallet_watch.execution_quality import (
    analyze_execution_quality,
    classify_liquidity_role,
    format_execution_quality_report,
)
from polymarket_wallet_watch.report import insert_wallet_event


def _conn(tmp_path):
    conn = connect(tmp_path / "watch.sqlite3")
    initialize_schema(conn)
    return conn


def _event(conn, *, trade_id, ts, price, token="yes", side="YES", action="buy", market="btc-updown-5m-1779129000", size=10):
    insert_wallet_event(
        conn,
        {
            "wallet_address": "0xabc",
            "market_id": "m1",
            "condition_id": "c1",
            "token_id": token,
            "event_ts": ts,
            "side": side,
            "action": action,
            "price": price,
            "size": size,
            "notional": round(price * size, 4),
            "source": "test",
            "market_slug": market,
            "market_title": "BTC Up/Down 5m",
            "trade_id": trade_id,
        },
    )


def _snap(conn, *, token="yes", ts, bid, ask):
    mid = (bid + ask) / 2
    conn.execute(
        """
        INSERT INTO market_snapshots(token_id, snapshot_ts, yes_bid, yes_ask, yes_mid, spread, source, raw_json)
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (token, ts, bid, ask, mid, ask - bid, "test", json.dumps({"bid": bid, "ask": ask})),
    )
    conn.commit()


def test_liquidity_role_classifies_maker_taker_inside_and_unknown():
    assert classify_liquidity_role("buy", 0.40, 0.40, 0.44, tolerance=0.001) == "maker"
    assert classify_liquidity_role("buy", 0.44, 0.40, 0.44, tolerance=0.001) == "taker"
    assert classify_liquidity_role("buy", 0.42, 0.40, 0.44, tolerance=0.001) == "inside_spread"
    assert classify_liquidity_role("buy", 0.42, None, 0.44, tolerance=0.001) == "unknown"


def test_execution_quality_handles_missing_snapshot_as_unknown(tmp_path):
    conn = _conn(tmp_path)
    _event(conn, trade_id="fill-1", ts="2026-05-18T18:30:14+00:00", price=0.40)

    summary = analyze_execution_quality(conn, wallet="0xabc", market_slug="btc-updown-5m-1779129000")

    assert summary["event_count"] == 1
    row = summary["events"][0]
    assert row["likely_liquidity_role"] == "unknown"
    assert row["fill_quality_tags"] == ["unknown_snapshot", "opening_chaos_fill"]
    assert row["pre_mid"] is None
    assert row["markout_5s"] is None


def test_execution_quality_calculates_markouts_and_tags_open_volatility_reversion(tmp_path):
    conn = _conn(tmp_path)
    _event(conn, trade_id="fill-1", ts="2026-05-18T18:30:14+00:00", price=0.40)
    _snap(conn, ts="2026-05-18T18:30:13+00:00", bid=0.40, ask=0.46)
    _snap(conn, ts="2026-05-18T18:30:19+00:00", bid=0.45, ask=0.47)
    _snap(conn, ts="2026-05-18T18:30:29+00:00", bid=0.47, ask=0.49)
    _snap(conn, ts="2026-05-18T18:31:14+00:00", bid=0.50, ask=0.52)

    summary = analyze_execution_quality(conn, wallet="0xabc", market_slug="btc-updown-5m-1779129000", snapshot_tolerance_seconds=2)
    row = summary["events"][0]

    assert row["pre_snapshot_ts"] == "2026-05-18T18:30:13+00:00"
    assert row["pre_best_bid"] == 0.40
    assert row["pre_best_ask"] == 0.46
    assert row["pre_mid"] == 0.43
    assert row["pre_spread"] == 0.06
    assert row["post_mid_5s"] == 0.46
    assert row["post_mid_15s"] == 0.48
    assert row["post_mid_60s"] == 0.51
    assert row["fill_vs_mid"] == -0.03
    assert row["markout_5s"] == 0.06
    assert row["markout_15s"] == 0.08
    assert row["markout_60s"] == 0.11
    assert row["seconds_after_open"] == 14.0
    assert row["phase"] == "open"
    assert row["likely_liquidity_role"] == "maker"
    assert "good_passive_fill" in row["fill_quality_tags"]
    assert "opening_chaos_fill" in row["fill_quality_tags"]
    assert "volatility_reversion_fill" in row["fill_quality_tags"]
    assert summary["role_counts"] == {"maker": 1, "taker": 0, "inside_spread": 0, "unknown": 0}
    assert summary["avg_markout_5s"] == 0.06


def test_execution_quality_report_aggregates_roles_phase_best_and_worst(tmp_path):
    conn = _conn(tmp_path)
    _event(conn, trade_id="good", ts="2026-05-18T18:30:14+00:00", price=0.40)
    _event(conn, trade_id="bad", ts="2026-05-18T18:32:00+00:00", price=0.55)
    _snap(conn, ts="2026-05-18T18:30:13+00:00", bid=0.40, ask=0.46)
    _snap(conn, ts="2026-05-18T18:30:19+00:00", bid=0.45, ask=0.47)
    _snap(conn, ts="2026-05-18T18:31:59+00:00", bid=0.50, ask=0.55)
    _snap(conn, ts="2026-05-18T18:32:05+00:00", bid=0.48, ask=0.50)

    summary = analyze_execution_quality(conn, wallet="0xabc", market_slug="btc-updown-5m-1779129000")
    text = format_execution_quality_report(summary)

    assert summary["event_count"] == 2
    assert summary["role_counts"]["maker"] == 1
    assert summary["role_counts"]["taker"] == 1
    assert summary["by_phase"]["open"]["count"] == 1
    assert summary["by_phase"]["mid"]["count"] == 1
    assert summary["best_fills_by_markout"][0]["trade_id"] == "good"
    assert summary["worst_fills_by_markout"][0]["trade_id"] == "bad"
    assert "Execution Quality" in text
    assert "maker=1 taker=1 inside_spread=0 unknown=0" in text
    assert "good fills cluster near open" in text
