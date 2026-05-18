from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from polymarket_wallet_watch.dashboard import build_dashboard_model, render_dashboard_html
from polymarket_wallet_watch.db import initialize_schema
from polymarket_wallet_watch.report import insert_wallet_event


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


def test_dashboard_model_is_read_only_and_structured() -> None:
    conn = _conn()
    insert_wallet_event(
        conn,
        {
            "wallet_address": "0xabc",
            "market_id": "m1",
            "condition_id": "c1",
            "token_id": "t1",
            "event_ts": "2026-05-18T18:00:00+00:00",
            "side": "YES",
            "action": "buy",
            "price": 0.42,
            "size": 10,
            "notional": 4.2,
            "source": "polymarket_clob",
            "market_slug": "btc-updown-15m-test",
            "market_title": "Bitcoin Up or Down",
        },
    )
    conn.execute(
        """
        INSERT INTO convergence_clusters(cluster_start_ts, cluster_end_ts, window_seconds, market_id, token_id, side,
                                         wallet_count, wallet_addresses, consensus_score, total_notional, leader_wallet)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        ("2026-05-18T18:00:00+00:00", "2026-05-18T18:00:15+00:00", 30, "m1", "t1", "YES", 2, "[\"0xabc\",\"0xdef\"]", 6.5, 25.0, "0xabc"),
    )
    conn.commit()

    model = build_dashboard_model(conn, config={"dashboard": {"refresh_seconds": 10}}, since="24h")

    assert model["safety"]["mode"] == "READ_ONLY_RESEARCH"
    assert model["api_neutral"] is True
    assert model["stats"]["wallet_events"] == 1
    assert model["recent_events"][0]["wallet_address"] == "0xabc"
    assert model["strongest_clusters"][0]["consensus_score"] == 6.5
    assert model["refresh_seconds"] == 10


def test_dashboard_html_renders_cards_and_safety_boundary() -> None:
    model = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "refresh_seconds": 15,
        "safety": {"mode": "READ_ONLY_RESEARCH", "no_private_keys": True, "no_orders": True, "no_live_trading": True},
        "api_neutral": True,
        "stats": {"wallets": 1, "wallet_events": 2, "clusters": 1, "markets": 3, "latest_event_ts": "2026-05-18T18:00:00+00:00"},
        "recent_events": [{"event_ts": "2026-05-18T18:00:00+00:00", "wallet_address": "0xabc", "market_slug": "btc-updown", "side": "YES", "action": "buy", "price": 0.42, "size": 10, "notional": 4.2}],
        "top_wallets": [{"wallet_address": "0xabc", "label": "leader", "event_count": 2, "notional": 8.4, "latest_event_ts": "2026-05-18T18:00:00+00:00", "likely_market_maker": False}],
        "strongest_clusters": [{"cluster_start_ts": "2026-05-18T18:00:00+00:00", "market_id": "m1", "side": "YES", "wallet_count": 2, "consensus_score": 6.5, "total_notional": 25.0, "leader_wallet": "0xabc"}],
        "active_markets": [{"slug": "btc-updown", "title": "BTC Up", "close_ts": "2026-05-18T18:15:00+00:00"}],
        "warnings": ["latency and liquidity matter"],
    }

    html = render_dashboard_html(model)

    assert "dashboard-shell" in html
    assert "hero-card" in html
    assert "portfolio-strip" in html
    assert "wallet-card" in html
    assert "cluster-card" in html
    assert "event-card" in html
    assert "READ_ONLY_RESEARCH" in html
    assert "API-neutral" in html
    assert "no private keys" in html.lower()
    assert "fetch('/api/dashboard')" in html
    assert "location.reload" not in html


def test_dashboard_model_keeps_per_wallet_stats_and_performance_metrics() -> None:
    conn = _conn()
    events = [
        ("0xabc", "YES", "buy", 0.40, 10, "m1", "btc-updown-a", "2026-05-18T18:00:00+00:00"),
        ("0xabc", "NO", "sell", 0.61, 5, "m2", "btc-updown-b", "2026-05-18T18:01:00+00:00"),
        ("0xabc", "YES", "buy", 0.44, 20, "m1", "btc-updown-a", "2026-05-18T18:02:00+00:00"),
    ]
    for wallet, side, action, price, size, market_id, slug, ts in events:
        insert_wallet_event(
            conn,
            {
                "wallet_address": wallet,
                "market_id": market_id,
                "condition_id": f"c-{market_id}",
                "token_id": f"t-{market_id}-{side}",
                "event_ts": ts,
                "side": side,
                "action": action,
                "price": price,
                "size": size,
                "notional": price * size,
                "source": "polymarket_clob",
                "market_slug": slug,
            },
        )
    conn.execute(
        """
        INSERT INTO wallet_alpha(wallet_address, asof_ts, event_count, avg_edge_15s, avg_edge_30s, avg_edge_60s,
                                 avg_edge_180s, win_rate_15s, win_rate_60s, sharpe_like_score,
                                 max_favorable_excursion, max_adverse_excursion, expiry_pnl, expiry_win_rate,
                                 likely_market_maker)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        ("0xabc", "2026-05-18T18:05:00+00:00", 3, 0.01, 0.015, 0.02, -0.005, 0.67, 0.75, 1.23, 0.08, -0.04, 0.12, 0.66, 0),
    )
    conn.commit()

    model = build_dashboard_model(conn, config={}, since="24h")
    wallet = model["wallet_performance"][0]

    assert wallet["wallet_address"] == "0xabc"
    assert wallet["event_count"] == 3
    assert wallet["buy_count"] == 2
    assert wallet["sell_count"] == 1
    assert wallet["yes_count"] == 2
    assert wallet["no_count"] == 1
    assert wallet["distinct_markets"] == 2
    assert wallet["avg_trade_notional"] == 5.2833
    assert wallet["avg_edge_60s"] == 0.02
    assert wallet["win_rate_60s"] == 0.75
    assert wallet["sharpe_like_score"] == 1.23
    assert wallet["max_favorable_excursion"] == 0.08
    assert wallet["max_adverse_excursion"] == -0.04
    assert wallet["expiry_pnl"] == 0.12
    assert wallet["expiry_win_rate"] == 0.66
    assert "wallet_performance" in model


def test_dashboard_html_renders_wallet_performance_metrics() -> None:
    model = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "refresh_seconds": 15,
        "safety": {"mode": "READ_ONLY_RESEARCH", "no_private_keys": True, "no_orders": True, "no_live_trading": True},
        "api_neutral": True,
        "stats": {"wallets": 1, "wallet_events": 3, "clusters": 0, "markets": 0, "latest_event_ts": "2026-05-18T18:02:00+00:00"},
        "recent_events": [],
        "top_wallets": [],
        "wallet_performance": [{"wallet_address": "0xabc", "label": "leader", "event_count": 3, "buy_count": 2, "sell_count": 1, "yes_count": 2, "no_count": 1, "distinct_markets": 2, "total_notional": 16.1, "avg_trade_notional": 5.3667, "avg_edge_15s": 0.01, "avg_edge_60s": 0.02, "win_rate_60s": 0.75, "sharpe_like_score": 1.23, "max_favorable_excursion": 0.08, "max_adverse_excursion": -0.04, "expiry_pnl": 0.12, "expiry_win_rate": 0.66, "likely_market_maker": False}],
        "strongest_clusters": [],
        "active_markets": [],
        "warnings": [],
    }

    html = render_dashboard_html(model)

    assert "Wallet performance" in html
    assert "performance-card" in html
    assert "Avg 60s edge" in html
    assert "Win 60s" in html
    assert "Sharpe-like" in html
    assert "MFE / MAE" in html
    assert "Expiry PnL" in html


def test_dashboard_model_keeps_per_wallet_market_positions_and_fills() -> None:
    conn = _conn()
    rows = [
        {"wallet_address": "0xabc", "market_id": "m1", "condition_id": "c1", "token_id": "yes-token", "event_ts": "2026-05-18T18:00:00+00:00", "side": "YES", "action": "buy", "price": 0.40, "size": 10, "notional": 4.0, "source": "polymarket_clob", "market_slug": "btc-updown-test", "trade_id": "fill-1"},
        {"wallet_address": "0xabc", "market_id": "m1", "condition_id": "c1", "token_id": "yes-token", "event_ts": "2026-05-18T18:01:00+00:00", "side": "YES", "action": "sell", "price": 0.45, "size": 4, "notional": 1.8, "source": "polymarket_clob", "market_slug": "btc-updown-test", "trade_id": "fill-2"},
        {"wallet_address": "0xabc", "market_id": "m1", "condition_id": "c1", "token_id": "no-token", "event_ts": "2026-05-18T18:02:00+00:00", "side": "NO", "action": "buy", "price": 0.52, "size": 3, "notional": 1.56, "source": "polymarket_clob", "market_slug": "btc-updown-test", "trade_id": "fill-3"},
        {"wallet_address": "0xdef", "market_id": "m2", "condition_id": "c2", "token_id": "other-token", "event_ts": "2026-05-18T18:03:00+00:00", "side": "YES", "action": "buy", "price": 0.30, "size": 7, "notional": 2.1, "source": "polymarket_clob", "market_slug": "eth-updown-test", "trade_id": "fill-4"},
    ]
    for row in rows:
        insert_wallet_event(conn, row)

    model = build_dashboard_model(conn, config={}, since="24h")
    positions = model["wallet_market_positions"]
    pos = next(p for p in positions if p["wallet_address"] == "0xabc" and p["market_key"] == "btc-updown-test")

    assert pos["yes_open_size"] == 6.0
    assert pos["no_open_size"] == 3.0
    assert pos["yes_avg_entry"] == 0.4
    assert pos["no_avg_entry"] == 0.52
    assert pos["yes_realized_pnl"] == 0.2
    assert pos["fill_count"] == 3
    assert len(pos["fills"] ) == 3
    assert pos["fills"][0]["trade_id"] == "fill-3"
    assert pos["fills"][0]["side"] == "NO"
    assert pos["fills"][1]["action"] == "sell"


def test_dashboard_html_renders_per_wallet_positions_and_fills() -> None:
    model = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "refresh_seconds": 15,
        "safety": {"mode": "READ_ONLY_RESEARCH", "no_private_keys": True, "no_orders": True, "no_live_trading": True},
        "api_neutral": True,
        "stats": {"wallets": 1, "wallet_events": 3, "clusters": 0, "markets": 0, "latest_event_ts": "2026-05-18T18:02:00+00:00"},
        "recent_events": [],
        "top_wallets": [],
        "wallet_performance": [],
        "wallet_market_positions": [{"wallet_address": "0xabc", "market_key": "btc-updown-test", "market_title": "BTC Up", "yes_open_size": 6.0, "no_open_size": 3.0, "yes_avg_entry": 0.4, "no_avg_entry": 0.52, "yes_realized_pnl": 0.2, "no_realized_pnl": 0.0, "fill_count": 3, "latest_event_ts": "2026-05-18T18:02:00+00:00", "fills": [{"event_ts": "2026-05-18T18:02:00+00:00", "side": "NO", "action": "buy", "price": 0.52, "size": 3, "notional": 1.56, "trade_id": "fill-3"}]}],
        "strongest_clusters": [],
        "active_markets": [],
        "warnings": [],
    }

    html = render_dashboard_html(model)

    assert "Per-wallet positions by market" in html
    assert "position-card" in html
    assert "YES open" in html
    assert "NO open" in html
    assert "Avg entries" in html
    assert "Realized PnL" in html
    assert "Recent fills" in html
    assert "fill-3" in html


def test_dashboard_model_reconciles_event_positions_against_wallet_position_snapshots() -> None:
    conn = _conn()
    insert_wallet_event(
        conn,
        {"wallet_address": "0xabc", "market_id": "m1", "condition_id": "c1", "token_id": "yes-token", "event_ts": "2026-05-18T18:00:00+00:00", "side": "YES", "action": "buy", "price": 0.40, "size": 10, "notional": 4.0, "source": "polymarket_clob", "market_slug": "btc-updown-15m-1779128100", "trade_id": "fill-1"},
    )
    conn.execute(
        """
        INSERT INTO wallet_positions(wallet_address, market_id, condition_id, token_id, side, position_size, avg_price,
                                     realized_pnl, unrealized_pnl, snapshot_ts, source, raw_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        ("0xabc", "m1", "c1", "yes-token", "YES", 8.0, 0.41, 0.12, 0.3, "2026-05-18T18:04:00+00:00", "polygon_readonly", "{}"),
    )
    conn.commit()

    model = build_dashboard_model(conn, config={}, since="24h")
    pos = model["wallet_market_positions"][0]

    assert pos["yes_open_size"] == 10.0
    assert pos["yes_reconciled_size"] == 8.0
    assert pos["yes_reconciliation_delta"] == -2.0
    assert pos["yes_reconciliation_source"] == "polygon_readonly"
    assert pos["yes_reconciliation_status"] == "mismatch"
    assert pos["reconciled"] is False
    assert pos["latest_reconciliation_ts"] == "2026-05-18T18:04:00+00:00"


def test_dashboard_model_deconstructs_strategy_per_contract_window() -> None:
    conn = _conn()
    rows = [
        {"wallet_address": "0xabc", "market_id": "m1", "condition_id": "c1", "token_id": "yes-token", "event_ts": "2026-05-18T18:00:05+00:00", "side": "YES", "action": "buy", "price": 0.40, "size": 10, "notional": 4.0, "source": "polymarket_clob", "market_slug": "btc-updown-15m-1779127200", "trade_id": "f1"},
        {"wallet_address": "0xabc", "market_id": "m1", "condition_id": "c1", "token_id": "no-token", "event_ts": "2026-05-18T18:01:00+00:00", "side": "NO", "action": "buy", "price": 0.50, "size": 9, "notional": 4.5, "source": "polymarket_clob", "market_slug": "btc-updown-15m-1779127200", "trade_id": "f2"},
        {"wallet_address": "0xabc", "market_id": "m1", "condition_id": "c1", "token_id": "yes-token", "event_ts": "2026-05-18T18:14:20+00:00", "side": "YES", "action": "sell", "price": 0.60, "size": 4, "notional": 2.4, "source": "polymarket_clob", "market_slug": "btc-updown-15m-1779127200", "trade_id": "f3"},
    ]
    for row in rows:
        insert_wallet_event(conn, row)

    model = build_dashboard_model(conn, config={}, since="24h")
    window = model["contract_window_strategies"][0]

    assert window["wallet_address"] == "0xabc"
    assert window["market_key"] == "btc-updown-15m-1779127200"
    assert window["window_seconds"] == 900
    assert window["first_trade_seconds_after_open"] == 5.0
    assert window["last_trade_seconds_after_open"] == 860.0
    assert window["entry_phase"] == "open"
    assert window["exit_phase"] == "late"
    assert window["strategy_tags"] == ["paired_yes_no", "late_reduce"]
    assert window["yes_bought"] == 10.0
    assert window["no_bought"] == 9.0
    assert window["yes_sold"] == 4.0
    assert window["fill_count"] == 3


def test_dashboard_html_renders_reconciliation_and_strategy_sections() -> None:
    model = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "refresh_seconds": 15,
        "safety": {"mode": "READ_ONLY_RESEARCH", "no_private_keys": True, "no_orders": True, "no_live_trading": True},
        "api_neutral": True,
        "stats": {"wallets": 1, "wallet_events": 3, "clusters": 0, "markets": 0, "latest_event_ts": "2026-05-18T18:02:00+00:00"},
        "recent_events": [],
        "top_wallets": [],
        "wallet_performance": [],
        "wallet_market_positions": [{"wallet_address": "0xabc", "market_key": "btc-updown", "yes_open_size": 10, "no_open_size": 9, "yes_avg_entry": 0.4, "no_avg_entry": 0.5, "yes_realized_pnl": 0.2, "no_realized_pnl": 0, "fill_count": 3, "latest_event_ts": "2026-05-18T18:02:00+00:00", "yes_reconciled_size": 8, "no_reconciled_size": 9, "yes_reconciliation_delta": -2, "no_reconciliation_delta": 0, "reconciled": False, "latest_reconciliation_ts": "2026-05-18T18:04:00+00:00", "fills": []}],
        "contract_window_strategies": [{"wallet_address": "0xabc", "market_key": "btc-updown", "entry_phase": "open", "exit_phase": "late", "strategy_tags": ["paired_yes_no", "late_reduce"], "fill_count": 3, "yes_bought": 10, "no_bought": 9, "yes_sold": 4, "no_sold": 0, "first_trade_seconds_after_open": 5, "last_trade_seconds_after_open": 860}],
        "execution_quality": [{"wallet_address": "0xabc", "market_slug": "btc-updown", "event_count": 3, "role_counts": {"maker": 2, "taker": 1, "inside_spread": 0, "unknown": 0}, "avg_markout_5s": 0.02, "avg_markout_15s": 0.03, "avg_markout_60s": 0.04, "by_phase": {"open": {"count": 2, "avg_markout_5s": 0.03}, "mid": {"count": 1, "avg_markout_5s": -0.01}}, "best_fills_by_markout": [{"event_ts": "2026-05-18T18:00:05+00:00", "side": "YES", "action": "buy", "fill_price": 0.4, "markout_5s": 0.05, "likely_liquidity_role": "maker", "fill_quality_tags": ["good_passive_fill"]}], "worst_fills_by_markout": []}],
        "polymarket_pair_feasibility": {"venue": "polymarket", "contracts": [{"market_key": "btc-updown-15m-1779130800", "best_pair_cost": 0.9, "best_pair_cost_ts": "2026-05-18T19:04:00+00:00", "best_pair_cost_phase": "mid", "best_visible_pair_depth": 180, "sizing_feasible": {"50": True, "200": False, "1000": False}}]},
        "passive_pair_builder": {"best_paper_windows_by_pair_cost": [{"market_key": "btc-updown-5m-1779129000", "first_quote_time": "2026-05-18T18:30:05+00:00", "first_fill_time": "2026-05-18T18:30:05+00:00", "yes_qty": 10, "no_qty": 10, "yes_avg_entry": 0.39, "no_avg_entry": 0.39, "matched_pair_qty": 10, "matched_pair_cost": 0.78, "locked_profit_if_held": 2.2, "unpaired_side": None, "unpaired_qty": 0, "open_phase_decisions": 4, "volatility_spike_decisions": 0, "repair_decisions": 0, "skipped_by_reason": {"spread_too_wide": 1}}], "skipped_opportunities_due_to_risk_rules": [{"market_key": "btc-updown-5m-1779129000", "skipped_by_reason": {"spread_too_wide": 1}}]},
        "strongest_clusters": [],
        "active_markets": [],
        "warnings": [],
    }

    html = render_dashboard_html(model)

    assert "Reconciliation" in html
    assert "reconciled" in html.lower()
    assert "Contract-window strategy deconstruction" in html
    assert "strategy-card" in html
    assert "paired_yes_no" in html
    assert "late_reduce" in html
    assert "Polymarket 1s Pair Feasibility" in html
    assert "btc-updown-15m-1779130800" in html
    assert "Passive Pair Builder" in html
    assert "pair-builder-card" in html
    assert "btc-updown-5m-1779129000" in html
    assert "maker=2" in html
    assert "Avg markouts" in html


def test_dashboard_json_serializable_without_network_calls(monkeypatch) -> None:
    import httpx

    def explode(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("dashboard must not call public APIs")

    monkeypatch.setattr(httpx.Client, "get", explode, raising=False)
    conn = _conn()

    model = build_dashboard_model(conn, config={}, since="24h")

    encoded = json.dumps(model)
    assert "READ_ONLY_RESEARCH" in encoded
    assert model["api_neutral"] is True
