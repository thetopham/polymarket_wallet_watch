from polymarket_wallet_watch.detect_convergence import build_convergence_clusters, compute_consensus_score


def test_compute_consensus_score_uses_alpha_direction_size_and_recency():
    events = [
        {"wallet_address": "a", "side": "YES", "action": "buy", "size": 100, "event_ts": "2026-05-18T12:00:00+00:00"},
        {"wallet_address": "b", "side": "YES", "action": "buy", "size": 25, "event_ts": "2026-05-18T12:00:10+00:00"},
        {"wallet_address": "c", "side": "YES", "action": "sell", "size": 25, "event_ts": "2026-05-18T12:00:12+00:00"},
    ]
    alpha = {"a": 2.0, "b": 1.0, "c": 1.0}

    score = compute_consensus_score(events, alpha, cluster_end_ts="2026-05-18T12:00:15+00:00", half_life_seconds=30)

    assert score > 0
    assert round(score, 3) == 13.931


def test_build_convergence_clusters_same_market_outcome_window():
    events = [
        {"id": 1, "wallet_address": "a", "market_id": "m", "token_id": "yes", "side": "YES", "action": "buy", "size": 10, "notional": 6, "event_ts": "2026-05-18T12:00:00+00:00"},
        {"id": 2, "wallet_address": "b", "market_id": "m", "token_id": "yes", "side": "YES", "action": "buy", "size": 20, "notional": 12, "event_ts": "2026-05-18T12:00:20+00:00"},
        {"id": 3, "wallet_address": "c", "market_id": "m", "token_id": "no", "side": "NO", "action": "buy", "size": 20, "notional": 10, "event_ts": "2026-05-18T12:00:22+00:00"},
        {"id": 4, "wallet_address": "d", "market_id": "m", "token_id": "yes", "side": "YES", "action": "buy", "size": 10, "notional": 6, "event_ts": "2026-05-18T12:02:00+00:00"},
    ]
    alpha = {"a": 1.5, "b": 1.0, "c": 3.0, "d": 1.0}

    clusters = build_convergence_clusters(events, alpha, window_seconds=30, min_wallets=2)

    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["wallet_count"] == 2
    assert cluster["market_id"] == "m"
    assert cluster["side"] == "YES"
    assert cluster["leader_wallet"] == "a"
    assert cluster["follower_lags_seconds"] == [20.0]
    assert cluster["total_notional"] == 18
    assert cluster["directional_agreement"] == 1.0
