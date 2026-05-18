import sqlite3

from polymarket_wallet_watch.score_wallets import compute_wallet_alpha_rows, flag_likely_market_makers


def test_compute_wallet_alpha_rows_forward_markouts_and_win_rate():
    events = [
        {"wallet_address": "a", "side": "YES", "price": 0.40, "mark_15s": 0.45, "mark_30s": 0.50, "mark_60s": 0.55, "mark_180s": 0.52, "expiry_price": 1.0},
        {"wallet_address": "a", "side": "YES", "price": 0.60, "mark_15s": 0.55, "mark_30s": 0.58, "mark_60s": 0.62, "mark_180s": 0.65, "expiry_price": 0.0},
        {"wallet_address": "b", "side": "NO", "price": 0.70, "mark_15s": 0.60, "mark_30s": 0.63, "mark_60s": 0.64, "mark_180s": 0.61, "expiry_price": 1.0},
    ]

    rows = compute_wallet_alpha_rows(events)
    by_wallet = {row["wallet_address"]: row for row in rows}

    assert by_wallet["a"]["event_count"] == 2
    assert by_wallet["a"]["win_rate_60s"] == 1.0
    assert round(by_wallet["a"]["avg_edge_60s"], 4) == 0.085
    assert round(by_wallet["b"]["avg_edge_15s"], 4) == -0.10
    assert by_wallet["b"]["expiry_win_rate"] == 1.0


def test_flag_likely_market_makers_when_both_sides_constant():
    events = []
    for i in range(12):
        events.append({"wallet_address": "mm", "side": "YES" if i % 2 == 0 else "NO", "action": "buy" if i % 3 else "sell"})
    events.append({"wallet_address": "alpha", "side": "YES", "action": "buy"})

    flags = flag_likely_market_makers(events, min_events=10, both_side_ratio=0.4, sell_ratio=0.2)

    assert flags["mm"] is True
    assert flags["alpha"] is False
