from __future__ import annotations

import argparse
import html
import json
import re
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import load_config
from .db import connect, initialize_schema
from .execution_quality import analyze_execution_quality
from .adapter_polymarket_1s import connect_feed as connect_polymarket_1s_feed
from .polymarket_pair_feasibility import scan_pair_feasibility


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _since_cutoff(since: str | None) -> str | None:
    if not since:
        return None
    text = since.strip().lower()
    if text.endswith("h") and text[:-1].isdigit():
        return (datetime.now(timezone.utc) - timedelta(hours=int(text[:-1]))).isoformat()
    return since


def _rows_to_dicts(rows: list[Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _fetch_one(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    return conn.execute(sql, params).fetchone()[0]


def build_dashboard_model(conn: Any, *, config: dict[str, Any] | None = None, since: str = "24h") -> dict[str, Any]:
    cfg = config or {}
    cutoff = _since_cutoff(since)
    where = "WHERE event_ts >= ?" if cutoff else ""
    params: tuple[Any, ...] = (cutoff,) if cutoff else ()
    refresh_seconds = int(cfg.get("dashboard", {}).get("refresh_seconds", 15))

    recent_events = _rows_to_dicts(
        conn.execute(
            f"""
            SELECT event_ts, wallet_address, market_slug, market_title, side, action, price, size, notional,
                   seconds_to_close, source
            FROM wallet_events
            {where}
            ORDER BY event_ts DESC, id DESC
            LIMIT 30
            """,
            params,
        ).fetchall()
    )
    top_wallets = _rows_to_dicts(
        conn.execute(
            f"""
            SELECT we.wallet_address, COALESCE(w.label, '') AS label, COUNT(*) AS event_count,
                   ROUND(COALESCE(SUM(we.notional), 0), 4) AS notional,
                   MAX(we.event_ts) AS latest_event_ts,
                   COALESCE((SELECT likely_market_maker FROM wallet_alpha wa
                             WHERE wa.wallet_address=we.wallet_address
                             ORDER BY asof_ts DESC LIMIT 1), 0) AS likely_market_maker
            FROM wallet_events we
            LEFT JOIN wallets w ON w.wallet_address=we.wallet_address
            {where.replace('event_ts', 'we.event_ts')}
            GROUP BY we.wallet_address
            ORDER BY latest_event_ts DESC, notional DESC
            LIMIT 12
            """,
            params,
        ).fetchall()
    )
    wallet_performance = _rows_to_dicts(
        conn.execute(
            f"""
            WITH latest_alpha AS (
                SELECT wa.*
                FROM wallet_alpha wa
                JOIN (
                    SELECT wallet_address, MAX(asof_ts) AS max_asof
                    FROM wallet_alpha
                    GROUP BY wallet_address
                ) latest
                  ON latest.wallet_address = wa.wallet_address
                 AND latest.max_asof = wa.asof_ts
            )
            SELECT we.wallet_address,
                   COALESCE(w.label, '') AS label,
                   COUNT(*) AS event_count,
                   SUM(CASE WHEN we.action='buy' THEN 1 ELSE 0 END) AS buy_count,
                   SUM(CASE WHEN we.action='sell' THEN 1 ELSE 0 END) AS sell_count,
                   SUM(CASE WHEN we.side='YES' THEN 1 ELSE 0 END) AS yes_count,
                   SUM(CASE WHEN we.side='NO' THEN 1 ELSE 0 END) AS no_count,
                   COUNT(DISTINCT COALESCE(we.market_id, we.market_slug, we.condition_id, we.token_id)) AS distinct_markets,
                   ROUND(COALESCE(SUM(we.notional), 0), 4) AS total_notional,
                   ROUND(COALESCE(AVG(we.notional), 0), 4) AS avg_trade_notional,
                   MIN(we.event_ts) AS first_event_ts,
                   MAX(we.event_ts) AS latest_event_ts,
                   ROUND(la.avg_edge_15s, 4) AS avg_edge_15s,
                   ROUND(la.avg_edge_30s, 4) AS avg_edge_30s,
                   ROUND(la.avg_edge_60s, 4) AS avg_edge_60s,
                   ROUND(la.avg_edge_180s, 4) AS avg_edge_180s,
                   ROUND(la.win_rate_15s, 4) AS win_rate_15s,
                   ROUND(la.win_rate_30s, 4) AS win_rate_30s,
                   ROUND(la.win_rate_60s, 4) AS win_rate_60s,
                   ROUND(la.win_rate_180s, 4) AS win_rate_180s,
                   ROUND(la.sharpe_like_score, 4) AS sharpe_like_score,
                   ROUND(la.max_favorable_excursion, 4) AS max_favorable_excursion,
                   ROUND(la.max_adverse_excursion, 4) AS max_adverse_excursion,
                   ROUND(la.expiry_pnl, 4) AS expiry_pnl,
                   ROUND(la.expiry_win_rate, 4) AS expiry_win_rate,
                   COALESCE(la.likely_market_maker, 0) AS likely_market_maker
            FROM wallet_events we
            LEFT JOIN wallets w ON w.wallet_address=we.wallet_address
            LEFT JOIN latest_alpha la ON la.wallet_address=we.wallet_address
            {where.replace('event_ts', 'we.event_ts')}
            GROUP BY we.wallet_address
            ORDER BY COALESCE(la.sharpe_like_score, la.avg_edge_60s, 0) DESC, total_notional DESC
            LIMIT 24
            """,
            params,
        ).fetchall()
    )
    strongest_clusters = _rows_to_dicts(
        conn.execute(
            """
            SELECT cluster_start_ts, market_id, side, wallet_count, ROUND(consensus_score, 4) AS consensus_score,
                   ROUND(COALESCE(total_notional, 0), 4) AS total_notional, leader_wallet,
                   forward_markout_60s
            FROM convergence_clusters
            ORDER BY ABS(consensus_score) DESC, cluster_start_ts DESC
            LIMIT 12
            """
        ).fetchall()
    )
    active_markets = _rows_to_dicts(
        conn.execute(
            """
            SELECT slug, title, close_ts, strike
            FROM markets
            WHERE COALESCE(active, 0)=1 AND COALESCE(closed, 0)=0
            ORDER BY close_ts ASC
            LIMIT 10
            """
        ).fetchall()
    )
    latest_event_ts = _fetch_one(conn, "SELECT MAX(event_ts) FROM wallet_events")
    stats = {
        "wallets": _fetch_one(conn, "SELECT COUNT(*) FROM wallets WHERE watch_enabled=1"),
        "markets": _fetch_one(conn, "SELECT COUNT(*) FROM markets"),
        "wallet_events": _fetch_one(conn, "SELECT COUNT(*) FROM wallet_events"),
        "clusters": _fetch_one(conn, "SELECT COUNT(*) FROM convergence_clusters"),
        "latest_event_ts": latest_event_ts,
    }
    return {
        "generated_at": _utc_now(),
        "refresh_seconds": refresh_seconds,
        "safety": {
            "mode": "READ_ONLY_RESEARCH",
            "no_private_keys": True,
            "no_orders": True,
            "no_live_trading": True,
        },
        "api_neutral": True,
        "stats": stats,
        "top_wallets": top_wallets,
        "wallet_performance": wallet_performance,
        "wallet_market_positions": build_wallet_market_positions(conn, since=since),
        "contract_window_strategies": build_contract_window_strategies(conn, since=since),
        "execution_quality": build_execution_quality_summaries(conn, since=since),
        "passive_pair_builder": build_passive_pair_builder_summaries(conn, since=since),
        "polymarket_pair_feasibility": build_polymarket_pair_feasibility(cfg),
        "strongest_clusters": strongest_clusters,
        "active_markets": active_markets,
        "recent_events": recent_events,
        "warnings": [
            "Latency, visible CLOB liquidity, external hedging, and wallet market-making can invalidate copy signals.",
            "Dashboard is API-neutral: it reads local SQLite only and does not call Polymarket, Polygon, or broker APIs.",
        ],
    }


def _esc(value: Any) -> str:
    if value is None:
        return "-"
    return html.escape(str(value), quote=True)


def _short_wallet(value: Any) -> str:
    text = str(value or "")
    if len(text) <= 14:
        return _esc(text)
    return _esc(f"{text[:8]}…{text[-6:]}")


def _card_metric(label: str, value: Any) -> str:
    return f'<div class="metric-card"><span>{_esc(label)}</span><strong>{_esc(value)}</strong></div>'


def _position_key(row: Any) -> tuple[str, str]:
    return (row["wallet_address"], row["market_slug"] or row["market_id"] or row["condition_id"] or row["token_id"] or "unknown")


def _signed_size(action: str | None, size: float | None) -> float:
    qty = float(size or 0)
    if action in {"sell", "reduce", "exit"}:
        return -qty
    return qty


def _avg_entry_cost(events: list[dict[str, Any]], side: str) -> float | None:
    buys = [e for e in events if e.get("side") == side and e.get("action") in {"buy", "add"} and e.get("size") and e.get("price") is not None]
    size = sum(float(e["size"]) for e in buys)
    if not size:
        return None
    return round(sum(float(e["size"]) * float(e["price"]) for e in buys) / size, 4)


def _realized_pnl_fifo(events: list[dict[str, Any]], side: str) -> float:
    lots: list[list[float]] = []
    pnl = 0.0
    for e in sorted((x for x in events if x.get("side") == side), key=lambda x: x.get("event_ts") or ""):
        qty = float(e.get("size") or 0)
        price = e.get("price")
        if not qty or price is None:
            continue
        px = float(price)
        action = e.get("action")
        if action in {"buy", "add"}:
            lots.append([qty, px])
        elif action in {"sell", "reduce", "exit"}:
            remaining = qty
            while remaining > 0 and lots:
                lot_qty, lot_px = lots[0]
                used = min(remaining, lot_qty)
                pnl += used * (px - lot_px)
                lot_qty -= used
                remaining -= used
                if lot_qty <= 1e-12:
                    lots.pop(0)
                else:
                    lots[0][0] = lot_qty
    return round(pnl, 4)


def _latest_reconciled_positions(conn: Any) -> dict[tuple[str, str, str], dict[str, Any]]:
    rows = _rows_to_dicts(
        conn.execute(
            """
            SELECT wp.*
            FROM wallet_positions wp
            JOIN (
                SELECT wallet_address, token_id, side, MAX(snapshot_ts) AS max_ts
                FROM wallet_positions
                GROUP BY wallet_address, token_id, side
            ) latest
              ON latest.wallet_address=wp.wallet_address
             AND latest.token_id=wp.token_id
             AND COALESCE(latest.side, '')=COALESCE(wp.side, '')
             AND latest.max_ts=wp.snapshot_ts
            """
        ).fetchall()
    )
    return {(r["wallet_address"], r.get("token_id") or "", r.get("side") or "UNKNOWN"): r for r in rows}


def _reconcile_side(position: dict[str, Any], latest: dict[tuple[str, str, str], dict[str, Any]], side: str, token_id: str | None, observed_size: float) -> None:
    snap = latest.get((position["wallet_address"], token_id or "", side))
    key = side.lower()
    if not snap:
        position[f"{key}_reconciled_size"] = None
        position[f"{key}_reconciliation_delta"] = None
        position[f"{key}_reconciliation_status"] = "missing"
        position[f"{key}_reconciliation_source"] = None
        return
    reconciled_size = float(snap.get("position_size") or 0)
    delta = round(reconciled_size - observed_size, 4)
    position[f"{key}_reconciled_size"] = round(reconciled_size, 4)
    position[f"{key}_reconciliation_delta"] = delta
    position[f"{key}_reconciliation_status"] = "matched" if abs(delta) < 1e-6 else "mismatch"
    position[f"{key}_reconciliation_source"] = snap.get("source")
    position[f"{key}_reconciled_avg_price"] = snap.get("avg_price")
    position[f"{key}_snapshot_realized_pnl"] = snap.get("realized_pnl")
    position[f"{key}_snapshot_unrealized_pnl"] = snap.get("unrealized_pnl")
    ts = snap.get("snapshot_ts")
    if ts and (not position.get("latest_reconciliation_ts") or ts > position["latest_reconciliation_ts"]):
        position["latest_reconciliation_ts"] = ts


def _infer_window_open_ts(market_key: str, fallback_ts: str | None) -> tuple[datetime | None, int | None]:
    m = re.search(r"-(\d{10})(?:\D|$)", market_key or "")
    if m:
        ts = int(m.group(1))
        return datetime.fromtimestamp(ts, tz=timezone.utc), 900 if "15m" in market_key else 300 if "5m" in market_key else 14400 if "4h" in market_key else None
    if fallback_ts:
        dt = _parse_iso(fallback_ts)
        if dt:
            return dt, None
    return None, None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _phase(seconds: float | None, window_seconds: int | None) -> str:
    if seconds is None or not window_seconds:
        return "unknown"
    ratio = seconds / window_seconds
    if ratio <= 0.2:
        return "open"
    if ratio >= 0.8:
        return "late"
    return "mid"


def _strategy_tags(yes_bought: float, no_bought: float, yes_sold: float, no_sold: float, first_phase: str, last_phase: str) -> list[str]:
    tags: list[str] = []
    if yes_bought > 0 and no_bought > 0:
        tags.append("paired_yes_no")
    elif yes_bought > 0:
        tags.append("directional_yes")
    elif no_bought > 0:
        tags.append("directional_no")
    if last_phase == "late" and (yes_sold > 0 or no_sold > 0):
        tags.append("late_reduce")
    elif yes_sold > 0 or no_sold > 0:
        tags.append("in_window_reduce")
    return tags


def build_contract_window_strategies(conn: Any, *, since: str = "24h", limit: int = 30) -> list[dict[str, Any]]:
    cutoff = _since_cutoff(since)
    where = "WHERE event_ts >= ?" if cutoff else ""
    params: tuple[Any, ...] = (cutoff,) if cutoff else ()
    rows = _rows_to_dicts(
        conn.execute(
            f"""
            SELECT wallet_address, market_id, condition_id, token_id, event_ts, side, action, price, size,
                   notional, market_slug, market_title, trade_id
            FROM wallet_events
            {where}
            ORDER BY event_ts ASC, id ASC
            """,
            params,
        ).fetchall()
    )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = row.get("market_slug") or row.get("market_id") or row.get("condition_id") or row.get("token_id") or "unknown"
        grouped.setdefault((row["wallet_address"], key), []).append(row)
    out: list[dict[str, Any]] = []
    for (wallet, market_key), events in grouped.items():
        opened, window_seconds = _infer_window_open_ts(market_key, events[0].get("event_ts"))
        offsets = []
        if opened:
            for e in events:
                dt = _parse_iso(e.get("event_ts"))
                if dt:
                    offsets.append((dt - opened).total_seconds())
        first_offset = round(offsets[0], 4) if offsets else None
        last_offset = round(offsets[-1], 4) if offsets else None
        yes_bought = sum(float(e.get("size") or 0) for e in events if e.get("side") == "YES" and e.get("action") in {"buy", "add"})
        no_bought = sum(float(e.get("size") or 0) for e in events if e.get("side") == "NO" and e.get("action") in {"buy", "add"})
        yes_sold = sum(float(e.get("size") or 0) for e in events if e.get("side") == "YES" and e.get("action") in {"sell", "reduce", "exit"})
        no_sold = sum(float(e.get("size") or 0) for e in events if e.get("side") == "NO" and e.get("action") in {"sell", "reduce", "exit"})
        entry_phase = _phase(first_offset, window_seconds)
        exit_phase = _phase(last_offset, window_seconds)
        out.append({
            "wallet_address": wallet,
            "market_key": market_key,
            "market_id": events[-1].get("market_id"),
            "market_title": events[-1].get("market_title"),
            "window_open_ts": opened.isoformat() if opened else None,
            "window_seconds": window_seconds,
            "first_trade_ts": events[0].get("event_ts"),
            "last_trade_ts": events[-1].get("event_ts"),
            "first_trade_seconds_after_open": first_offset,
            "last_trade_seconds_after_open": last_offset,
            "entry_phase": entry_phase,
            "exit_phase": exit_phase,
            "fill_count": len(events),
            "yes_bought": round(yes_bought, 4),
            "no_bought": round(no_bought, 4),
            "yes_sold": round(yes_sold, 4),
            "no_sold": round(no_sold, 4),
            "net_yes": round(yes_bought - yes_sold, 4),
            "net_no": round(no_bought - no_sold, 4),
            "total_notional": round(sum(float(e.get("notional") or 0) for e in events), 4),
            "strategy_tags": _strategy_tags(yes_bought, no_bought, yes_sold, no_sold, entry_phase, exit_phase),
        })
    out.sort(key=lambda r: (r.get("last_trade_ts") or "", r.get("total_notional") or 0), reverse=True)
    return out[:limit]


def build_wallet_market_positions(conn: Any, *, since: str = "24h", limit: int = 30, fills_per_position: int = 6) -> list[dict[str, Any]]:
    cutoff = _since_cutoff(since)
    where = "WHERE event_ts >= ?" if cutoff else ""
    params: tuple[Any, ...] = (cutoff,) if cutoff else ()
    rows = _rows_to_dicts(
        conn.execute(
            f"""
            SELECT id, wallet_address, market_id, condition_id, token_id, event_ts, side, action, price, size,
                   notional, market_slug, market_title, tx_hash, trade_id, source
            FROM wallet_events
            {where}
            ORDER BY event_ts ASC, id ASC
            """,
            params,
        ).fetchall()
    )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["wallet_address"], row.get("market_slug") or row.get("market_id") or row.get("condition_id") or row.get("token_id") or "unknown"), []).append(row)

    positions: list[dict[str, Any]] = []
    latest_reconciled = _latest_reconciled_positions(conn)
    for (wallet, market_key), events in grouped.items():
        yes_open = sum(_signed_size(e.get("action"), e.get("size")) for e in events if e.get("side") == "YES")
        no_open = sum(_signed_size(e.get("action"), e.get("size")) for e in events if e.get("side") == "NO")
        if abs(yes_open) < 1e-9 and abs(no_open) < 1e-9:
            continue
        fills = list(reversed(events))[:fills_per_position]
        yes_token = next((e.get("token_id") for e in reversed(events) if e.get("side") == "YES" and e.get("token_id")), None)
        no_token = next((e.get("token_id") for e in reversed(events) if e.get("side") == "NO" and e.get("token_id")), None)
        position = {
            "wallet_address": wallet,
            "market_key": market_key,
            "market_id": events[-1].get("market_id"),
            "condition_id": events[-1].get("condition_id"),
            "market_slug": events[-1].get("market_slug"),
            "market_title": events[-1].get("market_title"),
            "yes_open_size": round(yes_open, 4),
            "no_open_size": round(no_open, 4),
            "yes_avg_entry": _avg_entry_cost(events, "YES"),
            "no_avg_entry": _avg_entry_cost(events, "NO"),
            "yes_realized_pnl": _realized_pnl_fifo(events, "YES"),
            "no_realized_pnl": _realized_pnl_fifo(events, "NO"),
            "fill_count": len(events),
            "total_notional": round(sum(float(e.get("notional") or 0) for e in events), 4),
            "latest_event_ts": events[-1].get("event_ts"),
            "fills": [
                {
                    "event_ts": f.get("event_ts"),
                    "side": f.get("side"),
                    "action": f.get("action"),
                    "price": f.get("price"),
                    "size": f.get("size"),
                    "notional": f.get("notional"),
                    "trade_id": f.get("trade_id"),
                    "tx_hash": f.get("tx_hash"),
                    "source": f.get("source"),
                }
                for f in fills
            ],
        }
        _reconcile_side(position, latest_reconciled, "YES", yes_token, round(yes_open, 4))
        _reconcile_side(position, latest_reconciled, "NO", no_token, round(no_open, 4))
        position["reconciled"] = position.get("yes_reconciliation_status") in {"matched", "missing"} and position.get("no_reconciliation_status") in {"matched", "missing"}
        if position.get("yes_reconciliation_status") == "missing" and position.get("no_reconciliation_status") == "missing":
            position["reconciliation_state"] = "unverified"
        elif position["reconciled"]:
            position["reconciliation_state"] = "matched"
        else:
            position["reconciliation_state"] = "mismatch"
        positions.append(position)
    positions.sort(key=lambda p: (p.get("latest_event_ts") or "", p.get("total_notional") or 0), reverse=True)
    return positions[:limit]


def build_execution_quality_summaries(conn: Any, *, since: str = "24h", limit: int = 8) -> list[dict[str, Any]]:
    cutoff = _since_cutoff(since)
    where = "WHERE event_ts >= ?" if cutoff else ""
    params: tuple[Any, ...] = (cutoff,) if cutoff else ()
    pairs = _rows_to_dicts(
        conn.execute(
            f"""
            SELECT wallet_address, market_slug, MAX(event_ts) AS latest_event_ts, COUNT(*) AS event_count
            FROM wallet_events
            {where}
            GROUP BY wallet_address, market_slug
            ORDER BY latest_event_ts DESC, event_count DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    )
    out: list[dict[str, Any]] = []
    for pair in pairs:
        if not pair.get("market_slug"):
            continue
        try:
            summary = analyze_execution_quality(conn, wallet=pair["wallet_address"], market_slug=pair["market_slug"], since=since)
        except Exception as exc:  # dashboard must stay read-only and resilient to partial local data
            summary = {
                "wallet_address": pair["wallet_address"],
                "market_slug": pair["market_slug"],
                "event_count": pair.get("event_count", 0),
                "role_counts": {"maker": 0, "taker": 0, "inside_spread": 0, "unknown": pair.get("event_count", 0)},
                "avg_fill_vs_mid": None,
                "avg_markout_5s": None,
                "avg_markout_15s": None,
                "avg_markout_60s": None,
                "by_phase": {},
                "best_fills_by_markout": [],
                "worst_fills_by_markout": [],
                "error": str(exc),
            }
        out.append({
            "wallet_address": summary.get("wallet_address"),
            "market_slug": summary.get("market_slug"),
            "event_count": summary.get("event_count"),
            "role_counts": summary.get("role_counts", {}),
            "avg_fill_vs_mid": summary.get("avg_fill_vs_mid"),
            "avg_markout_5s": summary.get("avg_markout_5s"),
            "avg_markout_15s": summary.get("avg_markout_15s"),
            "avg_markout_60s": summary.get("avg_markout_60s"),
            "by_phase": summary.get("by_phase", {}),
            "best_fills_by_markout": summary.get("best_fills_by_markout", [])[:3],
            "worst_fills_by_markout": summary.get("worst_fills_by_markout", [])[:3],
            "error": summary.get("error"),
        })
    return out


def build_passive_pair_builder_summaries(conn: Any, *, since: str = "24h", limit: int = 12) -> dict[str, Any]:
    cutoff = _since_cutoff(since)
    where = "WHERE ts >= ?" if cutoff else ""
    params: tuple[Any, ...] = (cutoff,) if cutoff else ()
    rows = _rows_to_dicts(
        conn.execute(
            f"""
            SELECT *
            FROM paper_pair_builder_decisions
            {where}
            ORDER BY ts ASC, id ASC
            """,
            params,
        ).fetchall()
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = row.get("market_slug") or row.get("market_id") or "unknown"
        grouped.setdefault(key, []).append(row)
    windows: list[dict[str, Any]] = []
    for key, mrows in grouped.items():
        fills = [r for r in mrows if r.get("decision") == "fill_simulated"]
        latest = fills[-1] if fills else mrows[-1]
        skipped: dict[str, int] = {}
        for r in mrows:
            if r.get("decision") == "skip":
                reason = r.get("reason") or "unknown"
                skipped[reason] = skipped.get(reason, 0) + 1
        tags_blob = "\n".join(str(r.get("tags") or "") for r in mrows)
        windows.append({
            "market_key": key,
            "first_quote_time": next((r.get("ts") for r in mrows if r.get("decision") in {"quote", "repair"}), None),
            "first_fill_time": next((r.get("ts") for r in mrows if r.get("decision") == "fill_simulated"), None),
            "yes_qty": latest.get("yes_qty"),
            "no_qty": latest.get("no_qty"),
            "yes_avg_entry": latest.get("yes_avg_entry"),
            "no_avg_entry": latest.get("no_avg_entry"),
            "matched_pair_qty": latest.get("matched_pair_qty"),
            "matched_pair_cost": latest.get("matched_pair_cost"),
            "locked_profit_if_held": latest.get("locked_profit_if_held"),
            "unpaired_side": latest.get("unpaired_side"),
            "unpaired_qty": latest.get("unpaired_qty"),
            "max_imbalance": max(float(r.get("imbalance_ratio") or 0) for r in mrows),
            "open_phase_decisions": tags_blob.count("early_open_liquidity"),
            "volatility_spike_decisions": tags_blob.count("volatility_spike"),
            "repair_decisions": tags_blob.count("inventory_repair"),
            "skipped_by_reason": skipped,
            "decision_count": len(mrows),
            "fill_count": len(fills),
        })
    windows.sort(key=lambda r: (r.get("matched_pair_cost") is None, r.get("matched_pair_cost") or 999, -(r.get("matched_pair_qty") or 0)))
    return {
        "best_paper_windows_by_pair_cost": windows[:limit],
        "skipped_opportunities_due_to_risk_rules": [w for w in windows if w.get("skipped_by_reason")][:limit],
        "windows_where_wallet_achieved_better_fills_than_paper_bot": [],
        "windows_where_paper_bot_matched_wallet_behavior": [],
        "note": "Read-only SQLite paper simulator; no order routes or live trading.",
    }


def build_polymarket_pair_feasibility(config: dict[str, Any]) -> dict[str, Any]:
    path = config.get("polymarket_1s_feed", {}).get("sqlite_path") or "/home/matt/workspace/kalshi-btc-15m-bot/feed/polymarket-btc-1s.sqlite3"
    try:
        conn = connect_polymarket_1s_feed(path)
        try:
            return scan_pair_feasibility(conn, target_pair_cost=float(config.get("opening_window_pair_cost", {}).get("pair_cost_threshold", 0.95)), limit=5000)
        finally:
            conn.close()
    except Exception as exc:
        return {"venue": "polymarket", "contract_count": 0, "contracts": [], "error": str(exc), "feed_db": path}


def render_dashboard_html(model: dict[str, Any]) -> str:
    stats = model.get("stats", {})
    top_wallets = model.get("top_wallets", [])
    wallet_performance = model.get("wallet_performance", [])
    wallet_positions = model.get("wallet_market_positions", [])
    contract_strategies = model.get("contract_window_strategies", [])
    execution_quality = model.get("execution_quality", [])
    passive_pair_builder = model.get("passive_pair_builder", {})
    pair_feasibility = model.get("polymarket_pair_feasibility", {})
    clusters = model.get("strongest_clusters", [])
    events = model.get("recent_events", [])
    markets = model.get("active_markets", [])
    warnings = model.get("warnings", [])
    refresh = int(model.get("refresh_seconds") or 15)

    wallet_cards = "".join(
        f"""
        <article class="wallet-card">
          <div class="card-title">{_short_wallet(w.get('wallet_address'))}</div>
          <div class="muted">{_esc(w.get('label') or 'watched wallet')}</div>
          <div class="row"><span>Events</span><strong>{_esc(w.get('event_count'))}</strong></div>
          <div class="row"><span>Notional</span><strong>{_esc(w.get('notional'))}</strong></div>
          <div class="row"><span>Latest</span><strong>{_esc(w.get('latest_event_ts'))}</strong></div>
          <span class="badge {'warn' if w.get('likely_market_maker') else 'ok'}">{'IGNORE/MM?' if w.get('likely_market_maker') else 'watch'}</span>
        </article>
        """
        for w in top_wallets
    ) or '<article class="wallet-card empty">No wallet events yet.</article>'

    performance_cards = "".join(
        f"""
        <article class="performance-card wallet-card">
          <div class="card-title">{_short_wallet(w.get('wallet_address'))}</div>
          <div class="muted">{_esc(w.get('label') or 'watched wallet')} · markets={_esc(w.get('distinct_markets'))}</div>
          <div class="row"><span>Events</span><strong>{_esc(w.get('event_count'))}</strong></div>
          <div class="row"><span>Buys / sells</span><strong>{_esc(w.get('buy_count'))} / {_esc(w.get('sell_count'))}</strong></div>
          <div class="row"><span>YES / NO</span><strong>{_esc(w.get('yes_count'))} / {_esc(w.get('no_count'))}</strong></div>
          <div class="row"><span>Total notional</span><strong>{_esc(w.get('total_notional'))}</strong></div>
          <div class="row"><span>Avg trade</span><strong>{_esc(w.get('avg_trade_notional'))}</strong></div>
          <div class="row"><span>Avg 15s edge</span><strong>{_esc(w.get('avg_edge_15s'))}</strong></div>
          <div class="row"><span>Avg 60s edge</span><strong>{_esc(w.get('avg_edge_60s'))}</strong></div>
          <div class="row"><span>Win 60s</span><strong>{_esc(w.get('win_rate_60s'))}</strong></div>
          <div class="row"><span>Sharpe-like</span><strong>{_esc(w.get('sharpe_like_score'))}</strong></div>
          <div class="row"><span>MFE / MAE</span><strong>{_esc(w.get('max_favorable_excursion'))} / {_esc(w.get('max_adverse_excursion'))}</strong></div>
          <div class="row"><span>Expiry PnL</span><strong>{_esc(w.get('expiry_pnl'))}</strong></div>
          <span class="badge {'warn' if w.get('likely_market_maker') else 'ok'}">{'IGNORE/MM?' if w.get('likely_market_maker') else 'tracked'}</span>
        </article>
        """
        for w in wallet_performance
    ) or '<article class="performance-card wallet-card empty">No wallet performance metrics yet.</article>'

    position_cards = "".join(
        f"""
        <article class="position-card wallet-card">
          <div class="card-title">{_short_wallet(p.get('wallet_address'))}</div>
          <div class="muted">{_esc(p.get('market_key') or p.get('market_title'))}</div>
          <div class="row"><span>YES open</span><strong>{_esc(p.get('yes_open_size'))}</strong></div>
          <div class="row"><span>NO open</span><strong>{_esc(p.get('no_open_size'))}</strong></div>
          <div class="row"><span>Avg entries</span><strong>YES {_esc(p.get('yes_avg_entry'))} / NO {_esc(p.get('no_avg_entry'))}</strong></div>
          <div class="row"><span>Realized PnL</span><strong>YES {_esc(p.get('yes_realized_pnl'))} / NO {_esc(p.get('no_realized_pnl'))}</strong></div>
          <div class="row"><span>Reconciliation</span><strong>{_esc(p.get('reconciliation_state'))} @ {_esc(p.get('latest_reconciliation_ts'))}</strong></div>
          <div class="row"><span>Reconciled size</span><strong>YES {_esc(p.get('yes_reconciled_size'))} / NO {_esc(p.get('no_reconciled_size'))}</strong></div>
          <div class="row"><span>Reconcile delta</span><strong>YES {_esc(p.get('yes_reconciliation_delta'))} / NO {_esc(p.get('no_reconciliation_delta'))}</strong></div>
          <div class="row"><span>Fills</span><strong>{_esc(p.get('fill_count'))}</strong></div>
          <div class="row"><span>Latest</span><strong>{_esc(p.get('latest_event_ts'))}</strong></div>
          <details><summary>Recent fills</summary><ul>{''.join(f"<li>{_esc(f.get('event_ts'))} · {_esc(f.get('side'))} {_esc(f.get('action'))} px={_esc(f.get('price'))} size={_esc(f.get('size'))} notional={_esc(f.get('notional'))} id={_esc(f.get('trade_id'))}</li>" for f in p.get('fills', []))}</ul></details>
        </article>
        """
        for p in wallet_positions
    ) or '<article class="position-card wallet-card empty">No per-wallet market positions yet.</article>'

    strategy_cards = "".join(
        f"""
        <article class="strategy-card wallet-card">
          <div class="card-title">{_short_wallet(s.get('wallet_address'))}</div>
          <div class="muted">{_esc(s.get('market_key'))}</div>
          <div class="row"><span>Phases</span><strong>{_esc(s.get('entry_phase'))} → {_esc(s.get('exit_phase'))}</strong></div>
          <div class="row"><span>Trade timing</span><strong>{_esc(s.get('first_trade_seconds_after_open'))}s → {_esc(s.get('last_trade_seconds_after_open'))}s</strong></div>
          <div class="row"><span>YES bought/sold</span><strong>{_esc(s.get('yes_bought'))} / {_esc(s.get('yes_sold'))}</strong></div>
          <div class="row"><span>NO bought/sold</span><strong>{_esc(s.get('no_bought'))} / {_esc(s.get('no_sold'))}</strong></div>
          <div class="row"><span>Net YES / NO</span><strong>{_esc(s.get('net_yes'))} / {_esc(s.get('net_no'))}</strong></div>
          <div class="row"><span>Fills</span><strong>{_esc(s.get('fill_count'))}</strong></div>
          <div class="row"><span>Tags</span><strong>{_esc(', '.join(s.get('strategy_tags', [])))}</strong></div>
        </article>
        """
        for s in contract_strategies
    ) or '<article class="strategy-card wallet-card empty">No contract-window strategy summaries yet.</article>'

    execution_cards = "".join(
        f"""
        <article class="execution-card wallet-card">
          <div class="card-title">{_short_wallet(q.get('wallet_address'))}</div>
          <div class="muted">{_esc(q.get('market_slug'))}</div>
          <div class="row"><span>Events</span><strong>{_esc(q.get('event_count'))}</strong></div>
          <div class="row"><span>Role distribution</span><strong>maker={_esc(q.get('role_counts', {}).get('maker', 0))} taker={_esc(q.get('role_counts', {}).get('taker', 0))} inside_spread={_esc(q.get('role_counts', {}).get('inside_spread', 0))} unknown={_esc(q.get('role_counts', {}).get('unknown', 0))}</strong></div>
          <div class="row"><span>Avg fill vs mid</span><strong>{_esc(q.get('avg_fill_vs_mid'))}</strong></div>
          <div class="row"><span>Avg markouts</span><strong>5s {_esc(q.get('avg_markout_5s'))} / 15s {_esc(q.get('avg_markout_15s'))} / 60s {_esc(q.get('avg_markout_60s'))}</strong></div>
          <div class="row"><span>By phase</span><strong>{_esc(' · '.join(f'{phase}:{data.get("count", 0)} m5={data.get("avg_markout_5s")}' for phase, data in q.get('by_phase', {}).items()))}</strong></div>
          <details><summary>Best/worst fills</summary><ul>{''.join(f"<li>BEST {_esc(f.get('event_ts'))} {_esc(f.get('side'))} px={_esc(f.get('fill_price'))} m5={_esc(f.get('markout_5s'))} role={_esc(f.get('likely_liquidity_role'))}</li>" for f in q.get('best_fills_by_markout', []))}{''.join(f"<li>WORST {_esc(f.get('event_ts'))} {_esc(f.get('side'))} px={_esc(f.get('fill_price'))} m5={_esc(f.get('markout_5s'))} role={_esc(f.get('likely_liquidity_role'))}</li>" for f in q.get('worst_fills_by_markout', []))}</ul></details>
        </article>
        """
        for q in execution_quality
    ) or '<article class="execution-card wallet-card empty">No execution quality analysis yet. Run execution_quality after orderbook snapshot capture.</article>'

    passive_windows = passive_pair_builder.get("best_paper_windows_by_pair_cost", []) if isinstance(passive_pair_builder, dict) else []
    passive_skips = passive_pair_builder.get("skipped_opportunities_due_to_risk_rules", []) if isinstance(passive_pair_builder, dict) else []
    feasibility_contracts = pair_feasibility.get("contracts", []) if isinstance(pair_feasibility, dict) else []
    feasibility_cards = "".join(
        f"""
        <article class="pair-builder-card wallet-card">
          <div class="card-title">{_esc(c.get('market_key'))}</div>
          <div class="row"><span>Best pair cost</span><strong>{_esc(c.get('best_pair_cost'))}</strong></div>
          <div class="row"><span>Phase / time</span><strong>{_esc(c.get('best_pair_cost_phase'))} / {_esc(c.get('best_pair_cost_ts'))}</strong></div>
          <div class="row"><span>Visible pair depth</span><strong>{_esc(c.get('best_visible_pair_depth'))}</strong></div>
          <div class="row"><span>Sizing</span><strong>$50 {_esc(c.get('sizing_feasible', {}).get('50'))} / $200 {_esc(c.get('sizing_feasible', {}).get('200'))} / $1000 {_esc(c.get('sizing_feasible', {}).get('1000'))}</strong></div>
        </article>
        """
        for c in feasibility_contracts
    ) or '<article class="pair-builder-card wallet-card empty">No Polymarket 1s pair-feasibility rows yet.</article>'
    passive_cards = "".join(
        f"""
        <article class="pair-builder-card wallet-card">
          <div class="card-title">{_esc(w.get('market_key'))}</div>
          <div class="muted">paper-only pair inventory simulator</div>
          <div class="row"><span>First quote / fill</span><strong>{_esc(w.get('first_quote_time'))} / {_esc(w.get('first_fill_time'))}</strong></div>
          <div class="row"><span>YES qty / avg</span><strong>{_esc(w.get('yes_qty'))} / {_esc(w.get('yes_avg_entry'))}</strong></div>
          <div class="row"><span>NO qty / avg</span><strong>{_esc(w.get('no_qty'))} / {_esc(w.get('no_avg_entry'))}</strong></div>
          <div class="row"><span>Matched pair qty</span><strong>{_esc(w.get('matched_pair_qty'))}</strong></div>
          <div class="row"><span>Matched pair cost</span><strong>{_esc(w.get('matched_pair_cost'))}</strong></div>
          <div class="row"><span>Locked profit if held</span><strong>{_esc(w.get('locked_profit_if_held'))}</strong></div>
          <div class="row"><span>Unpaired</span><strong>{_esc(w.get('unpaired_side'))} {_esc(w.get('unpaired_qty'))}</strong></div>
          <div class="row"><span>Open / vol / repair decisions</span><strong>{_esc(w.get('open_phase_decisions'))} / {_esc(w.get('volatility_spike_decisions'))} / {_esc(w.get('repair_decisions'))}</strong></div>
          <details><summary>Skipped opportunities due to risk rules</summary><pre class="raw-json">{_esc(json.dumps(w.get('skipped_by_reason') or {}, sort_keys=True))}</pre></details>
        </article>
        """
        for w in passive_windows
    ) or '<article class="pair-builder-card wallet-card empty">No passive pair-builder paper decisions yet.</article>'

    cluster_cards = "".join(
        f"""
        <article class="cluster-card">
          <div class="card-title">{_esc(c.get('side'))} consensus · score {_esc(c.get('consensus_score'))}</div>
          <div class="muted">{_esc(c.get('cluster_start_ts'))}</div>
          <div class="row"><span>Wallets</span><strong>{_esc(c.get('wallet_count'))}</strong></div>
          <div class="row"><span>Total notional</span><strong>{_esc(c.get('total_notional'))}</strong></div>
          <div class="row"><span>Leader</span><strong>{_short_wallet(c.get('leader_wallet'))}</strong></div>
          <div class="row"><span>Market</span><strong>{_esc(c.get('market_id'))}</strong></div>
        </article>
        """
        for c in clusters
    ) or '<article class="cluster-card empty">No convergence clusters yet.</article>'

    event_cards = "".join(
        f"""
        <article class="event-card">
          <div class="card-title">{_esc(e.get('side'))} {_esc(e.get('action'))} · px={_esc(e.get('price'))}</div>
          <div class="muted">{_esc(e.get('event_ts'))}</div>
          <div>{_short_wallet(e.get('wallet_address'))}</div>
          <div>{_esc(e.get('market_slug') or e.get('market_title'))}</div>
          <div class="row"><span>Size</span><strong>{_esc(e.get('size'))}</strong></div>
          <div class="row"><span>Notional</span><strong>{_esc(e.get('notional'))}</strong></div>
        </article>
        """
        for e in events
    ) or '<article class="event-card empty">No recent wallet events.</article>'

    market_items = "".join(
        f"<li><strong>{_esc(m.get('slug') or m.get('title'))}</strong><span>{_esc(m.get('close_ts'))}</span></li>"
        for m in markets
    ) or "<li>No active markets in local cache.</li>"
    warning_items = "".join(f"<li>{_esc(w)}</li>" for w in warnings)
    metrics = "".join(
        [
            _card_metric("Wallets", stats.get("wallets", 0)),
            _card_metric("Events", stats.get("wallet_events", 0)),
            _card_metric("Clusters", stats.get("clusters", 0)),
            _card_metric("Markets", stats.get("markets", 0)),
        ]
    )

    boot_json = html.escape(json.dumps(model, default=str), quote=False)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta http-equiv="refresh" content="{refresh * 4}" />
<title>Polymarket Wallet Watch</title>
<style>
:root {{ color-scheme: dark; --bg:#080b12; --card:#111827; --muted:#8fa3bf; --text:#f3f7ff; --line:#233047; --green:#31d18b; --amber:#f5bd4f; --blue:#69a7ff; --red:#ff6b6b; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; background: radial-gradient(circle at top left, #14203a, var(--bg) 45%); color:var(--text); }}
.dashboard-shell {{ max-width:1180px; margin:0 auto; padding:22px; }}
.hero-card, .metric-card, .wallet-card, .performance-card, .position-card, .strategy-card, .execution-card, .pair-builder-card, .cluster-card, .event-card, .panel {{ background:rgba(17,24,39,.92); border:1px solid var(--line); border-radius:18px; box-shadow:0 18px 50px rgba(0,0,0,.25); }}
.hero-card {{ padding:22px; display:grid; gap:12px; margin-bottom:16px; }}
h1 {{ margin:0; font-size:clamp(1.7rem, 4vw, 3rem); }}
.subtitle, .muted {{ color:var(--muted); }}
.badge {{ display:inline-flex; width:max-content; border-radius:999px; padding:5px 10px; font-size:.78rem; font-weight:800; letter-spacing:.04em; text-transform:uppercase; background:#1f2c46; color:var(--blue); }}
.badge.ok {{ color:var(--green); }} .badge.warn {{ color:var(--amber); }} .badge.danger {{ color:var(--red); }}
.portfolio-strip {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:16px 0; }}
.metric-card {{ padding:16px; }} .metric-card span {{ color:var(--muted); display:block; }} .metric-card strong {{ font-size:1.6rem; }}
.grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; }}
.two-col {{ display:grid; grid-template-columns:1.2fr .8fr; gap:14px; }}
section {{ margin-top:18px; }}
h2 {{ margin:0 0 10px; }}
.wallet-card, .performance-card, .position-card, .strategy-card, .execution-card, .pair-builder-card, .cluster-card, .event-card, .panel {{ padding:15px; }}
.card-title {{ font-weight:800; margin-bottom:6px; }}
.row {{ display:flex; justify-content:space-between; gap:12px; border-top:1px solid rgba(143,163,191,.16); padding-top:8px; margin-top:8px; }}
ul {{ margin:0; padding-left:20px; }} li {{ margin:8px 0; }} li span {{ color:var(--muted); margin-left:8px; }}
.raw-json {{ white-space:pre-wrap; overflow:auto; max-height:420px; font-size:.78rem; color:#cbd5e1; }}
@media (max-width: 820px) {{ .portfolio-strip, .grid, .two-col {{ grid-template-columns:1fr; }} .dashboard-shell {{ padding:14px; }} }}
</style>
</head>
<body>
<main class="dashboard-shell">
  <header class="hero-card">
    <span class="badge ok">{_esc(model.get('safety', {}).get('mode', 'READ_ONLY_RESEARCH'))}</span>
    <h1>Polymarket Wallet Watch</h1>
    <div class="subtitle">Wallet convergence research dashboard. Safety: no private keys, no orders, no live trading.</div>
    <div class="subtitle">API-neutral: this dashboard reads local SQLite only; ingestion/replay commands perform public reads separately.</div>
    <div class="muted">Generated <span id="generatedAt">{_esc(model.get('generated_at'))}</span> · latest event {_esc(stats.get('latest_event_ts'))}</div>
  </header>
  <section class="portfolio-strip">{metrics}</section>
  <section><h2>Top wallets</h2><div class="grid" id="walletCards">{wallet_cards}</div></section>
  <section><h2>Wallet performance</h2><div class="grid" id="performanceCards">{performance_cards}</div></section>
  <section><h2>Per-wallet positions by market</h2><div class="grid" id="positionCards">{position_cards}</div></section>
  <section><h2>Contract-window strategy deconstruction</h2><div class="grid" id="strategyCards">{strategy_cards}</div></section>
  <section><h2>Execution Quality</h2><div class="grid" id="executionQualityCards">{execution_cards}</div></section>
  <section><h2>Polymarket 1s Pair Feasibility</h2><div class="grid" id="pairFeasibilityCards">{feasibility_cards}</div></section>
  <section><h2>Passive Pair Builder</h2><div class="grid" id="passivePairBuilderCards">{passive_cards}</div><div class="muted">Best paper windows by pair cost; wallet-better and behavior-match buckets are populated when wallet comparisons are run. Skipped risk windows: {_esc(len(passive_skips))}</div></section>
  <section><h2>Strongest convergence clusters</h2><div class="grid" id="clusterCards">{cluster_cards}</div></section>
  <section class="two-col">
    <div><h2>Recent wallet events</h2><div class="grid" id="eventCards">{event_cards}</div></div>
    <div class="panel"><h2>Active markets / warnings</h2><ul>{market_items}</ul><h3>Warnings</h3><ul>{warning_items}</ul></div>
  </section>
  <section class="panel"><details><summary>Raw local dashboard JSON</summary><pre class="raw-json" id="rawJson">{boot_json}</pre></details></section>
</main>
<script>
async function refreshDashboard() {{
  try {{
    const response = await fetch('/api/dashboard');
    if (!response.ok) return;
    const data = await response.json();
    document.getElementById('generatedAt').textContent = data.generated_at || '-';
    document.getElementById('rawJson').textContent = JSON.stringify(data, null, 2);
  }} catch (err) {{ console.warn('dashboard refresh failed', err); }}
}}
setInterval(refreshDashboard, {refresh * 1000});
</script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    db_path: Path
    config: dict[str, Any]
    since: str

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"dashboard {self.address_string()} - {fmt % args}")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _model(self, query: dict[str, list[str]]) -> dict[str, Any]:
        since = query.get("since", [self.since])[0]
        conn = connect(self.db_path)
        try:
            initialize_schema(conn)
            return build_dashboard_model(conn, config=self.config, since=since)
        finally:
            conn.close()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/healthz":
            body = json.dumps({"ok": True, "mode": "READ_ONLY_RESEARCH", "api_neutral": True}).encode()
            self._send(200, body, "application/json")
            return
        if parsed.path == "/api/dashboard":
            body = json.dumps(self._model(query), default=str).encode()
            self._send(200, body, "application/json")
            return
        if parsed.path in {"/", "/dashboard"}:
            html_body = render_dashboard_html(self._model(query)).encode()
            self._send(200, html_body, "text/html; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain")


def run_server(*, config_path: str, host: str, port: int, since: str) -> None:
    cfg = load_config(config_path)
    handler = type("ConfiguredDashboardHandler", (DashboardHandler,), {})
    handler.db_path = cfg.db_path
    handler.config = cfg.data
    handler.since = since
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Polymarket Wallet Watch dashboard READ_ONLY_RESEARCH on http://{host}:{port}")
    print("API-neutral: dashboard reads local SQLite only; no public API calls, no private keys, no orders.")
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve read-only local SQLite dashboard for Polymarket Wallet Watch.")
    parser.add_argument("--config", default="config.example.yaml")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8793)
    parser.add_argument("--since", default="24h")
    args = parser.parse_args()
    run_server(config_path=args.config, host=args.host, port=args.port, since=args.since)


if __name__ == "__main__":
    main()
