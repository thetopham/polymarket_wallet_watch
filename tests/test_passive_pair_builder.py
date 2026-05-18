from __future__ import annotations

import json
import sqlite3

from polymarket_wallet_watch.db import connect, initialize_schema
from polymarket_wallet_watch.passive_pair_builder import (
    Inventory,
    PassivePairBuilderConfig,
    Snapshot,
    compare_wallet_to_paper,
    format_passive_pair_builder_report,
    simulate_snapshots,
    simulate_resting_quote_snapshots,
    weighted_average_entry,
)
from polymarket_wallet_watch.report import insert_wallet_event


def _conn(tmp_path) -> sqlite3.Connection:
    conn = connect(tmp_path / "watch.sqlite3")
    initialize_schema(conn)
    return conn


def _snapshot(
    *,
    ts: str = "2026-05-18T18:30:05+00:00",
    market_slug: str = "btc-updown-5m-1779129000",
    seconds_after_open: float = 5,
    seconds_to_close: float = 295,
    yes_bid: float = 0.40,
    yes_ask: float = 0.44,
    no_bid: float = 0.52,
    no_ask: float = 0.56,
    yes_mid: float | None = None,
    no_mid: float | None = None,
    top_bid_size: float = 50,
    top_ask_size: float = 50,
    realized_vol_60s: float | None = None,
) -> Snapshot:
    return Snapshot(
        market_slug=market_slug,
        market_id="m1",
        token_id="yes-token",
        ts=ts,
        seconds_after_open=seconds_after_open,
        seconds_to_close=seconds_to_close,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        yes_mid=(yes_bid + yes_ask) / 2 if yes_mid is None else yes_mid,
        no_bid=no_bid,
        no_ask=no_ask,
        no_mid=(no_bid + no_ask) / 2 if no_mid is None else no_mid,
        top_bid_size=top_bid_size,
        top_ask_size=top_ask_size,
        realized_vol_60s=realized_vol_60s,
    )


def test_weighted_average_entry_calculation() -> None:
    assert weighted_average_entry(10, 0.50, 5, 0.35) == 0.45
    assert weighted_average_entry(0, None, 4, 0.25) == 0.25


def test_matched_pair_cost_and_locked_profit_if_held_calculation() -> None:
    inv = Inventory()
    inv.add_fill("YES", 10, 0.5447)
    inv.add_fill("NO", 8, 0.3566)

    assert inv.yes_avg_entry == 0.5447
    assert inv.no_avg_entry == 0.3566
    assert inv.matched_pair_qty == 8
    assert inv.matched_pair_cost == 0.9013
    assert inv.locked_edge_per_pair == 0.0987
    assert inv.locked_profit_if_held == 0.7896
    assert inv.unpaired_side == "YES"
    assert inv.unpaired_qty == 2


def test_projected_pair_cost_after_new_yes_fill() -> None:
    inv = Inventory(no_qty=10, no_notional=3.5)

    projected = inv.projected_pair_cost("YES", 5, 0.55)

    assert projected == 0.90


def test_projected_pair_cost_after_new_no_fill() -> None:
    inv = Inventory(yes_qty=10, yes_notional=5.4)

    projected = inv.projected_pair_cost("NO", 5, 0.36)

    assert projected == 0.90


def test_starter_inventory_allowed_near_open(tmp_path) -> None:
    conn = _conn(tmp_path)
    decisions = simulate_snapshots(conn, [_snapshot(seconds_after_open=10, yes_bid=0.40, no_bid=0.40, no_ask=0.44)], PassivePairBuilderConfig(open_window_seconds=60, edge_cents=3))

    quotes = [d for d in decisions if d["decision"] == "quote"]
    fills = [d for d in decisions if d["decision"] == "fill_simulated"]
    assert {d["side"] for d in quotes} == {"YES", "NO"}
    assert fills
    assert all("early_open_liquidity" in json.loads(d["tags"]) for d in fills)


def test_starter_inventory_blocked_outside_open_without_volatility(tmp_path) -> None:
    conn = _conn(tmp_path)
    decisions = simulate_snapshots(conn, [_snapshot(seconds_after_open=120, yes_bid=0.40, no_bid=0.40, realized_vol_60s=0.01)], PassivePairBuilderConfig(open_window_seconds=60))

    assert not [d for d in decisions if d["decision"] == "fill_simulated"]
    assert any(d["reason"] == "starter_blocked_outside_open_without_volatility" for d in decisions)


def test_unpaired_exposure_cap(tmp_path) -> None:
    conn = _conn(tmp_path)
    cfg = PassivePairBuilderConfig(open_window_seconds=60, max_unpaired_qty=5, max_imbalance_ratio=999, order_size=10)
    decisions = simulate_snapshots(conn, [_snapshot(seconds_after_open=5)], cfg)

    assert not [d for d in decisions if d["decision"] == "fill_simulated"]
    assert any(d["reason"] == "max_unpaired_qty" for d in decisions)


def test_prefers_underweight_side_when_inventory_is_imbalanced(tmp_path) -> None:
    conn = _conn(tmp_path)
    inv = Inventory(yes_qty=20, yes_notional=8.0, no_qty=10, no_notional=4.0)
    cfg = PassivePairBuilderConfig(order_size=5, max_unpaired_qty=100, max_imbalance_ratio=10)

    decisions = simulate_snapshots(conn, [_snapshot(seconds_after_open=120, realized_vol_60s=0.08, no_bid=0.38, no_ask=0.42)], cfg, inventory=inv)

    fills = [d for d in decisions if d["decision"] == "fill_simulated"]
    assert [d["side"] for d in fills] == ["NO"]
    assert any(d["reason"] == "prefer_underweight_side" and d["side"] == "YES" for d in decisions)


def test_pair_cost_target_blocks_non_repair_adds(tmp_path) -> None:
    conn = _conn(tmp_path)
    inv = Inventory(yes_qty=10, yes_notional=5.5)
    cfg = PassivePairBuilderConfig(target_pair_cost=0.95, hard_pair_cost_ceiling=0.99, order_size=10)

    decisions = simulate_snapshots(conn, [_snapshot(seconds_after_open=120, no_bid=0.43, no_ask=0.47, realized_vol_60s=0.08)], cfg, inventory=inv)

    assert not [d for d in decisions if d["decision"] == "fill_simulated"]
    assert any(d["reason"] == "projected_pair_cost_too_high" for d in decisions)


def test_hard_pair_cost_ceiling_blocks_repair_near_close(tmp_path) -> None:
    conn = _conn(tmp_path)
    inv = Inventory(yes_qty=20, yes_notional=10.0, no_qty=5, no_notional=2.0)
    cfg = PassivePairBuilderConfig(repair_only_seconds_before_close=60, hard_pair_cost_ceiling=0.99, order_size=5)

    decisions = simulate_snapshots(conn, [_snapshot(seconds_after_open=260, seconds_to_close=40, no_bid=0.62, no_ask=0.66)], cfg, inventory=inv)

    assert not [d for d in decisions if d["decision"] == "fill_simulated"]
    assert any(d["reason"] == "projected_pair_cost_too_high" and d["side"] == "NO" for d in decisions)


def test_notional_caps_default_to_two_thousand_total_one_thousand_per_side() -> None:
    cfg = PassivePairBuilderConfig()

    assert cfg.max_contract_notional == 2000.0
    assert cfg.max_side_notional == 1000.0
    assert cfg.max_pair_notional == 2000.0
    assert cfg.target_pair_cost == 0.95
    assert cfg.hard_pair_cost_ceiling == 0.99


def test_repair_only_mode_buys_smaller_side(tmp_path) -> None:
    conn = _conn(tmp_path)
    inv = Inventory(yes_qty=20, yes_notional=10.0, no_qty=5, no_notional=2.0)
    cfg = PassivePairBuilderConfig(repair_only_seconds_before_close=60, hard_pair_cost_ceiling=0.99, order_size=5)

    decisions = simulate_snapshots(conn, [_snapshot(seconds_after_open=260, seconds_to_close=40, no_bid=0.38, no_ask=0.42)], cfg, inventory=inv)

    fills = [d for d in decisions if d["decision"] == "fill_simulated"]
    assert [d["side"] for d in fills] == ["NO"]
    assert fills[0]["decision"] == "fill_simulated"
    assert "inventory_repair" in json.loads(fills[0]["tags"])


def test_skip_when_spread_too_wide(tmp_path) -> None:
    conn = _conn(tmp_path)
    decisions = simulate_snapshots(conn, [_snapshot(yes_bid=0.20, yes_ask=0.40, no_bid=0.40, no_ask=0.70)], PassivePairBuilderConfig(max_spread=0.05))

    assert not [d for d in decisions if d["decision"] == "fill_simulated"]
    assert any(d["reason"] == "spread_too_wide" for d in decisions)


def test_decisions_are_persisted_to_paper_pair_builder_decisions(tmp_path) -> None:
    conn = _conn(tmp_path)
    simulate_snapshots(conn, [_snapshot()], PassivePairBuilderConfig())

    count = conn.execute("SELECT COUNT(*) FROM paper_pair_builder_decisions").fetchone()[0]
    row = conn.execute("SELECT market_slug, side, decision, quote_price, matched_pair_cost FROM paper_pair_builder_decisions ORDER BY id DESC LIMIT 1").fetchone()
    assert count > 0
    assert row["market_slug"] == "btc-updown-5m-1779129000"
    assert row["decision"] in {"quote", "fill_simulated", "skip", "repair"}


def test_compare_wallet_vs_paper_strategy_similarity(tmp_path) -> None:
    conn = _conn(tmp_path)
    for trade_id, side, price, size, ts in [
        ("w1", "YES", 0.54, 10, "2026-05-18T18:30:10+00:00"),
        ("w2", "NO", 0.36, 8, "2026-05-18T18:30:50+00:00"),
    ]:
        insert_wallet_event(
            conn,
            {
                "wallet_address": "0xabc",
                "market_id": "m1",
                "condition_id": "c1",
                "token_id": f"{side.lower()}-token",
                "event_ts": ts,
                "side": side,
                "action": "buy",
                "price": price,
                "size": size,
                "notional": price * size,
                "source": "test",
                "market_slug": "btc-updown-5m-1779129000",
                "trade_id": trade_id,
            },
        )
    inv = Inventory()
    inv.add_fill("YES", 10, 0.55)
    inv.add_fill("NO", 8, 0.35)

    comparison = compare_wallet_to_paper(conn, wallet="0xabc", market_slug="btc-updown-5m-1779129000", inventory=inv, first_fill_ts="2026-05-18T18:30:12+00:00")

    assert comparison["wallet_yes_avg"] == 0.54
    assert comparison["paper_yes_avg"] == 0.55
    assert comparison["wallet_pair_cost"] == 0.90
    assert comparison["paper_pair_cost"] == 0.90
    assert comparison["wallet_matched_pair_qty"] == 8
    assert comparison["paper_matched_pair_qty"] == 8
    assert comparison["wallet_unpaired_side"] == "YES"
    assert comparison["paper_unpaired_side"] == "YES"
    assert comparison["timing_difference_seconds"] == 2
    assert comparison["similarity_score"] > 0.8


def test_resting_quote_places_once_and_fills_on_later_touch(tmp_path) -> None:
    conn = _conn(tmp_path)
    cfg = PassivePairBuilderConfig(order_size=10, quote_stale_seconds=30, edge_cents=1)
    snapshots = [
        _snapshot(ts="2026-05-18T18:30:05+00:00", seconds_after_open=5, yes_bid=0.40, yes_ask=0.44, no_bid=0.40, no_ask=0.44),
        _snapshot(ts="2026-05-18T18:30:06+00:00", seconds_after_open=6, yes_bid=0.39, yes_ask=0.40, no_bid=0.40, no_ask=0.44),
        _snapshot(ts="2026-05-18T18:30:07+00:00", seconds_after_open=7, yes_bid=0.39, yes_ask=0.44, no_bid=0.39, no_ask=0.40),
    ]

    result = simulate_resting_quote_snapshots(conn, snapshots, cfg)
    fills = [d for d in result["events"] if d["decision"] == "fill_simulated"]

    assert result["metrics"].quotes_placed >= 2
    assert [f["side"] for f in fills[:2]] == ["YES", "NO"]
    assert result["inventory"].matched_pair_cost <= 0.95
    assert result["metrics"].quote_touches >= 2


def test_resting_quote_does_not_refill_same_quote_every_tick(tmp_path) -> None:
    conn = _conn(tmp_path)
    cfg = PassivePairBuilderConfig(order_size=10, quote_stale_seconds=30, edge_cents=1, max_imbalance_ratio=10)
    snapshots = [
        _snapshot(ts="2026-05-18T18:30:05+00:00", seconds_after_open=5, yes_bid=0.40, yes_ask=0.44, no_bid=0.40, no_ask=0.44),
        _snapshot(ts="2026-05-18T18:30:06+00:00", seconds_after_open=6, yes_bid=0.39, yes_ask=0.40, no_bid=0.40, no_ask=0.44),
        _snapshot(ts="2026-05-18T18:30:07+00:00", seconds_after_open=7, yes_bid=0.39, yes_ask=0.40, no_bid=0.40, no_ask=0.44),
    ]

    result = simulate_resting_quote_snapshots(conn, snapshots, cfg)
    yes_fills = [d for d in result["events"] if d["decision"] == "fill_simulated" and d["side"] == "YES"]

    assert len(yes_fills) == 1


def test_resting_quote_cancels_stale_quote_before_replace(tmp_path) -> None:
    conn = _conn(tmp_path)
    cfg = PassivePairBuilderConfig(order_size=10, quote_stale_seconds=1, edge_cents=1)
    snapshots = [
        _snapshot(ts="2026-05-18T18:30:05+00:00", seconds_after_open=5, yes_bid=0.40, yes_ask=0.44, no_bid=0.40, no_ask=0.44),
        _snapshot(ts="2026-05-18T18:30:07+00:00", seconds_after_open=7, yes_bid=0.40, yes_ask=0.44, no_bid=0.40, no_ask=0.44),
    ]

    result = simulate_resting_quote_snapshots(conn, snapshots, cfg)

    assert any(d["decision"] == "cancel" and d["reason"] == "quote_stale_cancel_replace" for d in result["events"])


def test_resting_quote_report_includes_lifecycle_metrics(tmp_path) -> None:
    conn = _conn(tmp_path)
    cfg = PassivePairBuilderConfig(order_size=10, quote_stale_seconds=30, edge_cents=1)
    snapshots = [
        _snapshot(ts="2026-05-18T18:30:05+00:00", seconds_after_open=5, yes_bid=0.40, yes_ask=0.44, no_bid=0.40, no_ask=0.44),
        _snapshot(ts="2026-05-18T18:30:06+00:00", seconds_after_open=6, yes_bid=0.39, yes_ask=0.40, no_bid=0.40, no_ask=0.44),
    ]
    simulate_resting_quote_snapshots(conn, snapshots, cfg)

    text = format_passive_pair_builder_report(conn, market_slug="btc-updown-5m-1779129000")

    assert "total quotes placed" in text
    assert "simulated fills" in text
    assert "quote cancellations" in text
    assert "reached matched notional" in text

def test_format_report_includes_passive_pair_builder_sections(tmp_path) -> None:
    conn = _conn(tmp_path)
    simulate_snapshots(conn, [_snapshot(), _snapshot(ts="2026-05-18T18:30:20+00:00", seconds_after_open=20, yes_bid=0.50, no_bid=0.38, realized_vol_60s=0.08)], PassivePairBuilderConfig())

    text = format_passive_pair_builder_report(conn, market_slug="btc-updown-5m-1779129000")

    assert "Passive Pair Builder Report" in text
    assert "READ-ONLY/PAPER-ONLY" in text
    assert "matched pair cost" in text.lower()
    assert "open-phase decisions" in text
    assert "volatility-spike decisions" in text
    assert "skipped decisions by reason" in text
