from __future__ import annotations

import math

from polymarket_wallet_watch.probability_model import (
    ProbabilityModelConfig,
    enrich_probability_features,
    format_probability_report,
    model_yes_probability,
    summarize_probability_features,
)


def test_model_yes_probability_uses_distance_time_and_volatility() -> None:
    prob, z = model_yes_probability(distance_from_strike=20, seconds_to_close=100, sigma_per_sqrt_second=2)

    assert z == 1.0
    assert math.isclose(prob, 0.841345, abs_tol=1e-6)


def test_model_probability_floor_prevents_zero_vol_explosion() -> None:
    prob, z = model_yes_probability(distance_from_strike=-10, seconds_to_close=100, sigma_per_sqrt_second=0, cfg=ProbabilityModelConfig(vol_floor_per_sqrt_second=2))

    assert z == -0.5
    assert math.isclose(prob, 0.308538, abs_tol=1e-6)


def test_enrich_probability_features_computes_edges_and_signals() -> None:
    snaps = []
    for i, price in enumerate([100.0, 101.0, 102.0, 103.0, 104.0]):
        snaps.append({
            "ts": f"2026-05-18T18:30:0{i}+00:00",
            "market_slug": "btc-updown-15m-test",
            "market_key": "btc-updown-15m-test",
            "btc_price": price,
            "strike": 100.0,
            "seconds_to_close": 100 - i,
            "yes_bid": 0.50,
            "yes_ask": 0.52,
            "no_bid": 0.48,
            "no_ask": 0.50,
        })

    rows = enrich_probability_features(snaps, cfg=ProbabilityModelConfig(edge_threshold=0.03, vol_floor_per_sqrt_second=1.0))
    latest = rows[-1]

    assert latest["distance_from_strike"] == 4.0
    assert latest["model_yes_probability"] is not None
    assert latest["market_yes_mid"] == 0.51
    assert latest["yes_edge"] is not None
    assert latest["yes_quote_signal"] is True
    assert latest["no_quote_signal"] is False


def test_summary_and_report_are_read_only_and_include_latest_context() -> None:
    rows = enrich_probability_features([
        {"ts": "2026-05-18T18:30:00+00:00", "market_slug": "m", "market_key": "m", "btc_price": 99.0, "strike": 100.0, "seconds_to_close": 60, "yes_bid": 0.40, "yes_ask": 0.42, "no_bid": 0.58, "no_ask": 0.60},
        {"ts": "2026-05-18T18:30:01+00:00", "market_slug": "m", "market_key": "m", "btc_price": 101.0, "strike": 100.0, "seconds_to_close": 59, "yes_bid": 0.40, "yes_ask": 0.42, "no_bid": 0.58, "no_ask": 0.60},
    ], cfg=ProbabilityModelConfig(edge_threshold=0.01))

    summary = summarize_probability_features(rows, cfg=ProbabilityModelConfig(edge_threshold=0.01))
    text = format_probability_report(summary, rows)

    assert summary["usable_snapshot_count"] == 2
    assert summary["safety"].startswith("READ_ONLY_RESEARCH")
    assert "Probability Model" in text
    assert "distance=" in text
    assert "no private keys" in text.lower()
