from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any

from .adapter_polymarket_1s import DEFAULT_POLYMARKET_1S_DB, connect_feed, load_nearest_snapshot
from .config import load_config
from .db import connect, initialize_schema, insert_dict
from .probability_model import ProbabilityModelConfig, enrich_probability_features

ANALYSIS_VERSION = "wallet_probability_edge_v1"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _since_cutoff(since: str | None) -> str | None:
    if not since:
        return None
    text = since.strip().lower()
    if text.endswith("h") and text[:-1].isdigit():
        return (datetime.now(timezone.utc) - timedelta(hours=int(text[:-1]))).isoformat()
    if text.endswith("d") and text[:-1].isdigit():
        return (datetime.now(timezone.utc) - timedelta(days=int(text[:-1]))).isoformat()
    return since


def _round(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _fetch_wallet_events(conn: sqlite3.Connection, *, wallet: str, since: str | None, asset: str | None, interval: str | None, limit: int | None) -> list[sqlite3.Row]:
    clauses = ["LOWER(wallet_address)=LOWER(?)", "side IN ('YES','NO')", "action IN ('buy','add')"]
    params: list[Any] = [wallet]
    cutoff = _since_cutoff(since)
    if cutoff:
        clauses.append("event_ts >= ?")
        params.append(cutoff)
    if asset:
        clauses.append("LOWER(COALESCE(market_slug,'')) LIKE ?")
        params.append(f"{asset.lower()}%")
    if interval:
        clauses.append("LOWER(COALESCE(market_slug,'')) LIKE ?")
        params.append(f"%-{interval.lower()}-%")
    sql_limit = "LIMIT ?" if limit else ""
    if limit:
        params.append(limit)
    return conn.execute(
        f"""
        SELECT id, wallet_address, market_slug, market_id, condition_id, token_id, event_ts,
               side, action, price, size, notional, trade_id, tx_hash
        FROM wallet_events
        WHERE {' AND '.join(clauses)}
        ORDER BY event_ts ASC, id ASC
        {sql_limit}
        """,
        tuple(params),
    ).fetchall()


def _event_probability_row(feed_conn: sqlite3.Connection, event: sqlite3.Row, *, tolerance_seconds: float, cfg: ProbabilityModelConfig) -> dict[str, Any]:
    market_key = event["market_slug"] or event["market_id"] or event["condition_id"]
    snap = load_nearest_snapshot(feed_conn, event["event_ts"], market_key=market_key, tolerance_seconds=tolerance_seconds)
    prob = enrich_probability_features([snap], cfg=cfg)[0] if snap else {}
    side = event["side"]
    market_mid = prob.get("market_yes_mid") if side == "YES" else prob.get("market_no_mid")
    model_prob = prob.get("model_yes_probability") if side == "YES" else prob.get("model_no_probability")
    edge = prob.get("yes_edge") if side == "YES" else prob.get("no_edge")
    fill_price = float(event["price"]) if event["price"] is not None else None
    fill_edge = (model_prob - fill_price) if model_prob is not None and fill_price is not None else None
    classification = "unknown_no_snapshot"
    if edge is not None:
        if edge >= cfg.edge_threshold:
            classification = "positive_model_edge"
        elif edge <= -cfg.edge_threshold:
            classification = "negative_model_edge"
        else:
            classification = "near_fair"
    return {
        "event_id": int(event["id"]),
        "wallet_address": event["wallet_address"],
        "market_slug": event["market_slug"],
        "event_ts": event["event_ts"],
        "side": side,
        "action": event["action"],
        "fill_price": fill_price,
        "size": float(event["size"] or 0),
        "notional": float(event["notional"] or 0),
        "nearest_snapshot_ts": snap.get("ts") if snap else None,
        "snapshot_distance_seconds": snap.get("distance_seconds") if snap else None,
        "btc_price": prob.get("btc_price"),
        "strike": prob.get("strike"),
        "distance_from_strike": prob.get("distance_from_strike"),
        "seconds_to_close": prob.get("seconds_to_close"),
        "realized_vol_60s": prob.get("realized_vol_60s"),
        "trend_slope_30s": prob.get("trend_slope_30s"),
        "z_score": prob.get("z_score"),
        "model_yes_probability": prob.get("model_yes_probability"),
        "model_no_probability": prob.get("model_no_probability"),
        "market_yes_mid": prob.get("market_yes_mid"),
        "market_no_mid": prob.get("market_no_mid"),
        "model_side_probability": model_prob,
        "market_side_mid": market_mid,
        "model_edge_vs_mid": edge,
        "model_edge_vs_fill": _round(fill_edge),
        "classification": classification,
        "analysis_version": ANALYSIS_VERSION,
    }


def _classify_role(row: dict[str, Any], *, yes_qty_before: float, no_qty_before: float, edge_threshold: float, hedge_max_price: float) -> str:
    side = row.get("side")
    edge_fill = row.get("model_edge_vs_fill")
    fill_price = row.get("fill_price")
    reduces_imbalance = (side == "YES" and yes_qty_before < no_qty_before) or (side == "NO" and no_qty_before < yes_qty_before)
    if edge_fill is not None and edge_fill >= edge_threshold:
        return "core_directional"
    if fill_price is not None and fill_price <= hedge_max_price:
        return "cheap_hedge"
    if reduces_imbalance and (edge_fill is None or edge_fill > -0.20):
        return "inventory_repair"
    return "bad_fill_or_noise"


def _apply_portfolio_timeline(rows: list[dict[str, Any]], *, edge_threshold: float, hedge_max_price: float) -> None:
    yes_qty = no_qty = yes_notional = no_notional = 0.0
    for row in rows:
        side = row.get("side")
        size = float(row.get("size") or 0)
        fill_price = float(row.get("fill_price") or 0)
        role = _classify_role(row, yes_qty_before=yes_qty, no_qty_before=no_qty, edge_threshold=edge_threshold, hedge_max_price=hedge_max_price)
        if side == "YES":
            yes_qty += size
            yes_notional += size * fill_price
        elif side == "NO":
            no_qty += size
            no_notional += size * fill_price
        model_yes = row.get("model_yes_probability")
        model_no = row.get("model_no_probability")
        portfolio_model_value = None
        if model_yes is not None and model_no is not None:
            portfolio_model_value = yes_qty * float(model_yes) + no_qty * float(model_no)
        portfolio_cost = yes_notional + no_notional
        row["wallet_role"] = role
        row["yes_qty_after"] = _round(yes_qty)
        row["no_qty_after"] = _round(no_qty)
        row["yes_notional_after"] = _round(yes_notional)
        row["no_notional_after"] = _round(no_notional)
        row["portfolio_cost"] = _round(portfolio_cost)
        row["portfolio_model_value"] = _round(portfolio_model_value)
        row["portfolio_edge"] = _round(portfolio_model_value - portfolio_cost if portfolio_model_value is not None else None)
        row["portfolio_imbalance_qty"] = _round(abs(yes_qty - no_qty))


def _inventory_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    yes_qty = yes_notional = no_qty = no_notional = 0.0
    for row in rows:
        side = row.get("side")
        size = float(row.get("size") or 0)
        price = float(row.get("fill_price") or 0)
        if side == "YES":
            yes_qty += size
            yes_notional += size * price
        elif side == "NO":
            no_qty += size
            no_notional += size * price
    yes_avg = yes_notional / yes_qty if yes_qty else None
    no_avg = no_notional / no_qty if no_qty else None
    matched = min(yes_qty, no_qty)
    pair_cost = (yes_avg + no_avg) if yes_avg is not None and no_avg is not None else None
    return {
        "yes_qty": _round(yes_qty),
        "yes_avg": _round(yes_avg),
        "yes_notional": _round(yes_notional),
        "no_qty": _round(no_qty),
        "no_avg": _round(no_avg),
        "no_notional": _round(no_notional),
        "matched_pair_qty": _round(matched),
        "pair_cost": _round(pair_cost),
        "imbalance_qty": _round(abs(yes_qty - no_qty)),
    }


def _market_key(row: dict[str, Any]) -> str:
    return row.get("market_slug") or "unknown"


def _apply_portfolio_timeline_by_market(rows: list[dict[str, Any]], *, edge_threshold: float, hedge_max_price: float) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(_market_key(row), []).append(row)
    summaries: dict[str, dict[str, Any]] = {}
    for key, mrows in grouped.items():
        _apply_portfolio_timeline(mrows, edge_threshold=edge_threshold, hedge_max_price=hedge_max_price)
        with_snapshot = [r for r in mrows if r.get("nearest_snapshot_ts")]
        role_counts: dict[str, int] = {}
        class_counts = {"positive_model_edge": 0, "near_fair": 0, "negative_model_edge": 0, "unknown_no_snapshot": 0}
        for r in mrows:
            role = r.get("wallet_role") or "unknown"
            role_counts[role] = role_counts.get(role, 0) + 1
            cls = r.get("classification") or "unknown_no_snapshot"
            class_counts[cls] = class_counts.get(cls, 0) + 1
        edges = [float(r["portfolio_edge"]) for r in mrows if r.get("portfolio_edge") is not None]
        inv = _inventory_stats(mrows)
        first = mrows[0] if mrows else {}
        last = mrows[-1] if mrows else {}
        summaries[key] = {
            "market_slug": key,
            "events": len(mrows),
            "snapshot_matched": len(with_snapshot),
            **inv,
            "final_portfolio_model_value": last.get("portfolio_model_value"),
            "final_portfolio_cost": last.get("portfolio_cost"),
            "final_portfolio_edge": last.get("portfolio_edge"),
            "max_portfolio_edge": _round(max(edges) if edges else None),
            "min_portfolio_edge": _round(min(edges) if edges else None),
            "final_imbalance_qty": inv["imbalance_qty"],
            "role_counts": role_counts,
            "positive_count": class_counts.get("positive_model_edge", 0),
            "near_fair_count": class_counts.get("near_fair", 0),
            "negative_count": class_counts.get("negative_model_edge", 0),
            "first_fill_time": first.get("event_ts"),
            "last_fill_time": last.get("event_ts"),
            "seconds_after_open_first_fill": None,
            "held_to_expiry": "unknown",
        }
    return summaries


def _create_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wallet_probability_edge_events (
            event_id INTEGER PRIMARY KEY,
            wallet_address TEXT NOT NULL,
            market_slug TEXT,
            event_ts TEXT NOT NULL,
            side TEXT,
            action TEXT,
            fill_price REAL,
            size REAL,
            notional REAL,
            nearest_snapshot_ts TEXT,
            snapshot_distance_seconds REAL,
            btc_price REAL,
            strike REAL,
            distance_from_strike REAL,
            seconds_to_close REAL,
            realized_vol_60s REAL,
            trend_slope_30s REAL,
            z_score REAL,
            model_yes_probability REAL,
            model_no_probability REAL,
            market_yes_mid REAL,
            market_no_mid REAL,
            model_side_probability REAL,
            market_side_mid REAL,
            model_edge_vs_mid REAL,
            model_edge_vs_fill REAL,
            classification TEXT,
            wallet_role TEXT,
            yes_qty_after REAL,
            no_qty_after REAL,
            yes_notional_after REAL,
            no_notional_after REAL,
            portfolio_cost REAL,
            portfolio_model_value REAL,
            portfolio_edge REAL,
            portfolio_imbalance_qty REAL,
            analysis_version TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_probability_edge_wallet ON wallet_probability_edge_events(wallet_address, market_slug, event_ts)")
    conn.commit()


def _persist(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    _create_table(conn)
    for row in rows:
        cols = list(row.keys())
        assignments = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "event_id")
        conn.execute(
            f"""
            INSERT INTO wallet_probability_edge_events({','.join(cols)})
            VALUES({','.join(['?'] * len(cols))})
            ON CONFLICT(event_id) DO UPDATE SET {assignments}
            """,
            tuple(row[c] for c in cols),
        )
    conn.commit()


def analyze_wallet_probability_edges(
    wallet_conn: sqlite3.Connection,
    *,
    feed_db: str,
    wallet: str,
    since: str | None = "24h",
    asset: str | None = "btc",
    interval: str | None = "15m",
    edge_threshold: float = 0.03,
    tolerance_seconds: float = 2.0,
    hedge_max_price: float = 0.20,
    limit: int | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    cfg = ProbabilityModelConfig(edge_threshold=edge_threshold)
    events = _fetch_wallet_events(wallet_conn, wallet=wallet, since=since, asset=asset, interval=interval, limit=limit)
    feed_conn = connect_feed(feed_db)
    try:
        rows = [_event_probability_row(feed_conn, event, tolerance_seconds=tolerance_seconds, cfg=cfg) for event in events]
    finally:
        feed_conn.close()
    _apply_portfolio_timeline_by_market(rows, edge_threshold=edge_threshold, hedge_max_price=hedge_max_price)
    if persist:
        _persist(wallet_conn, rows)
    with_snapshot = [r for r in rows if r["nearest_snapshot_ts"]]
    pos = [r for r in with_snapshot if r["classification"] == "positive_model_edge"]
    neg = [r for r in with_snapshot if r["classification"] == "negative_model_edge"]
    near = [r for r in with_snapshot if r["classification"] == "near_fair"]
    yes_rows = [r for r in with_snapshot if r["side"] == "YES"]
    no_rows = [r for r in with_snapshot if r["side"] == "NO"]
    def avg(key: str, seq: list[dict[str, Any]]) -> float | None:
        vals = [float(r[key]) for r in seq if r.get(key) is not None]
        return round(mean(vals), 6) if vals else None
    role_counts: dict[str, int] = {}
    for r in with_snapshot:
        role = r.get("wallet_role") or "unknown"
        role_counts[role] = role_counts.get(role, 0) + 1
    market_summaries = _apply_portfolio_timeline_by_market(rows, edge_threshold=edge_threshold, hedge_max_price=hedge_max_price)
    final_edges = [float(m["final_portfolio_edge"]) for m in market_summaries.values() if m.get("final_portfolio_edge") is not None]
    pair_costs = [float(m["pair_cost"]) for m in market_summaries.values() if m.get("pair_cost") is not None]
    imbalances = [float(m["final_imbalance_qty"] or 0) for m in market_summaries.values()]
    def med(vals: list[float]) -> float | None:
        vals = sorted(vals)
        if not vals:
            return None
        mid = len(vals) // 2
        if len(vals) % 2:
            return round(vals[mid], 6)
        return round((vals[mid - 1] + vals[mid]) / 2, 6)
    return {
        "wallet_address": wallet.lower(),
        "feed_db": feed_db,
        "since": since,
        "asset": asset,
        "interval": interval,
        "event_count": len(rows),
        "with_snapshot_count": len(with_snapshot),
        "missing_snapshot_count": len(rows) - len(with_snapshot),
        "positive_model_edge_count": len(pos),
        "negative_model_edge_count": len(neg),
        "near_fair_count": len(near),
        "positive_model_edge_notional": round(sum(r["notional"] for r in pos), 6),
        "yes_avg_edge_vs_fill": avg("model_edge_vs_fill", yes_rows),
        "no_avg_edge_vs_fill": avg("model_edge_vs_fill", no_rows),
        "avg_snapshot_distance_seconds": avg("snapshot_distance_seconds", with_snapshot),
        "role_counts": role_counts,
        "market_summaries": list(market_summaries.values()),
        "markets_analyzed": len(market_summaries),
        "markets_positive_final_edge": sum(1 for e in final_edges if e > 0),
        "markets_negative_final_edge": sum(1 for e in final_edges if e < 0),
        "median_final_portfolio_edge": med(final_edges),
        "median_pair_cost": med(pair_costs),
        "median_imbalance": med(imbalances),
        "latest_portfolio_cost": None,
        "latest_portfolio_model_value": None,
        "latest_portfolio_edge": None,
        "latest_yes_qty": None,
        "latest_no_qty": None,
        "latest_portfolio_imbalance_qty": None,
        "rows": rows,
        "safety": "READ_ONLY_RESEARCH: no private keys, no signers, no orders, no live trading.",
    }


def format_wallet_probability_edge_report(summary: dict[str, Any], *, limit: int = 20) -> str:
    rows = summary.get("rows", [])
    lines = [
        "Wallet Probability Edge Report — READ-ONLY RESEARCH",
        "Safety: local wallet SQLite + local Polymarket 1s feed only; no private keys, no signers, no orders.",
        "Purpose: classify whether watched-wallet fills were cheap vs distance/time/volatility model, not to claim profitability.",
        f"Wallet: {summary.get('wallet_address')}",
        f"Filters: since={summary.get('since')} asset={summary.get('asset')} interval={summary.get('interval')}",
        f"Events: {summary.get('event_count')} snapshot_matched={summary.get('with_snapshot_count')} missing_snapshot={summary.get('missing_snapshot_count')}",
        f"Classification: positive={summary.get('positive_model_edge_count')} near_fair={summary.get('near_fair_count')} negative={summary.get('negative_model_edge_count')}",
        f"Positive-edge notional: {summary.get('positive_model_edge_notional')}",
        f"Avg edge vs fill: YES={summary.get('yes_avg_edge_vs_fill')} NO={summary.get('no_avg_edge_vs_fill')}",
        f"Role counts: {summary.get('role_counts')}",
        f"Aggregate markets: analyzed={summary.get('markets_analyzed')} positive_final_edge={summary.get('markets_positive_final_edge')} negative_final_edge={summary.get('markets_negative_final_edge')}",
        f"Aggregate medians: final_edge={summary.get('median_final_portfolio_edge')} pair_cost={summary.get('median_pair_cost')} imbalance={summary.get('median_imbalance')}",
        f"Avg snapshot distance seconds: {summary.get('avg_snapshot_distance_seconds')}",
        "",
        "Per-market portfolio summaries:",
    ]
    for m in summary.get("market_summaries", []):
        lines.append(
            f"  {m.get('market_slug')} events={m.get('events')} YES={m.get('yes_qty')}@{m.get('yes_avg')} NO={m.get('no_qty')}@{m.get('no_avg')} "
            f"matched={m.get('matched_pair_qty')} pair_cost={m.get('pair_cost')} final_edge={m.get('final_portfolio_edge')} "
            f"max_edge={m.get('max_portfolio_edge')} min_edge={m.get('min_portfolio_edge')} imbalance={m.get('final_imbalance_qty')} "
            f"roles={m.get('role_counts')} classes=+{m.get('positive_count')}/~{m.get('near_fair_count')}/-{m.get('negative_count')} "
            f"first={m.get('first_fill_time')} last={m.get('last_fill_time')} held_to_expiry={m.get('held_to_expiry')}"
        )
    lines += [
        "",
        "Recent fills with probability context:",
    ]
    for r in rows[-limit:]:
        lines.append(
            f"  {r.get('event_ts')} {r.get('market_slug')} {r.get('side')} px={r.get('fill_price')} size={r.get('size')} "
            f"role={r.get('wallet_role')} class={r.get('classification')} edge_mid={r.get('model_edge_vs_mid')} edge_fill={r.get('model_edge_vs_fill')} "
            f"portfolio_edge={r.get('portfolio_edge')} portfolio_cost={r.get('portfolio_cost')} "
            f"model_side={r.get('model_side_probability')} mid={r.get('market_side_mid')} z={r.get('z_score')} "
            f"d={r.get('distance_from_strike')} t={r.get('seconds_to_close')} snap_dt={r.get('snapshot_distance_seconds')}"
        )
    if not rows:
        lines.append("  No matching wallet buy/add events for this filter.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Join watched wallet fills to local Polymarket 1s probability model snapshots.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--feed-db", default=None)
    parser.add_argument("--wallet", required=True)
    parser.add_argument("--since", default="24h")
    parser.add_argument("--asset", default="btc")
    parser.add_argument("--interval", default="15m")
    parser.add_argument("--edge-threshold", type=float, default=0.03)
    parser.add_argument("--snapshot-tolerance-seconds", type=float, default=2.0)
    parser.add_argument("--hedge-max-price", type=float, default=0.20)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-persist", action="store_true")
    args = parser.parse_args()
    cfg = load_config(args.config)
    feed_db = args.feed_db or cfg.data.get("polymarket_1s_feed", {}).get("sqlite_path") or str(DEFAULT_POLYMARKET_1S_DB)
    conn = connect(cfg.db_path)
    initialize_schema(conn)
    summary = analyze_wallet_probability_edges(
        conn,
        feed_db=feed_db,
        wallet=args.wallet,
        since=args.since,
        asset=args.asset,
        interval=args.interval,
        edge_threshold=args.edge_threshold,
        tolerance_seconds=args.snapshot_tolerance_seconds,
        hedge_max_price=args.hedge_max_price,
        limit=args.limit,
        persist=not args.no_persist,
    )
    print(format_wallet_probability_edge_report(summary))


if __name__ == "__main__":
    main()
