from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any

from .config import load_config
from .db import connect, initialize_schema

ANALYSIS_VERSION = "execution_quality_v1"
ROLES = ("maker", "taker", "inside_spread", "unknown")
PHASES = ("open", "mid", "late", "unknown")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


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
    if not math.isfinite(value):
        return None
    return round(float(value), digits)


def _market_open(market_slug: str | None, fallback_ts: str | None = None) -> tuple[datetime | None, int | None]:
    slug = market_slug or ""
    match = re.search(r"-(5m|15m|1h)-(\d{9,12})$", slug)
    if match:
        interval = match.group(1)
        window = {"5m": 300, "15m": 900, "1h": 3600}.get(interval)
        return datetime.fromtimestamp(int(match.group(2)), tz=timezone.utc), window
    return _parse_ts(fallback_ts), None


def _phase(seconds_after_open: float | None, window_seconds: int | None) -> str:
    if seconds_after_open is None:
        return "unknown"
    if seconds_after_open <= 60:
        return "open"
    if window_seconds and seconds_after_open >= max(window_seconds - 60, window_seconds * 0.8):
        return "late"
    return "mid"


def classify_liquidity_role(
    action: str | None,
    fill_price: float | None,
    pre_best_bid: float | None,
    pre_best_ask: float | None,
    *,
    tolerance: float = 0.001,
) -> str:
    if fill_price is None or pre_best_bid is None or pre_best_ask is None:
        return "unknown"
    action_l = (action or "").lower()
    if action_l in {"buy", "add"}:
        if fill_price <= pre_best_bid + tolerance:
            return "maker"
        if fill_price >= pre_best_ask - tolerance:
            return "taker"
        if pre_best_bid + tolerance < fill_price < pre_best_ask - tolerance:
            return "inside_spread"
    if action_l in {"sell", "reduce", "exit"}:
        if fill_price >= pre_best_ask - tolerance:
            return "maker"
        if fill_price <= pre_best_bid + tolerance:
            return "taker"
        if pre_best_bid + tolerance < fill_price < pre_best_ask - tolerance:
            return "inside_spread"
    return "unknown"


def _direction(action: str | None) -> int:
    return -1 if (action or "").lower() in {"sell", "reduce", "exit"} else 1


def _nearest_snapshot(
    conn: sqlite3.Connection,
    *,
    token_id: str | None,
    target_ts: str,
    before: bool | None,
    tolerance_seconds: float | None = None,
) -> sqlite3.Row | None:
    if not token_id:
        return None
    target = _parse_ts(target_ts)
    if not target:
        return None
    if before is True:
        op = "<="
        order = "DESC"
        params: list[Any] = [token_id, target_ts]
        extra = ""
    elif before is False:
        op = ">="
        order = "ASC"
        params = [token_id, target_ts]
        extra = ""
    else:
        op = ">="
        order = "ASC"
        params = [token_id, (target - timedelta(seconds=tolerance_seconds or 0)).isoformat(), (target + timedelta(seconds=tolerance_seconds or 0)).isoformat()]
        extra = "AND snapshot_ts <= ?"
    if before is None:
        row = conn.execute(
            """
            SELECT *, ABS((julianday(snapshot_ts) - julianday(?)) * 86400.0) AS distance_seconds
            FROM market_snapshots
            WHERE token_id=? AND snapshot_ts >= ? AND snapshot_ts <= ?
            ORDER BY distance_seconds ASC, snapshot_ts DESC
            LIMIT 1
            """,
            (target_ts, token_id, params[1], params[2]),
        ).fetchone()
        return row
    row = conn.execute(
        f"""
        SELECT *
        FROM market_snapshots
        WHERE token_id=? AND snapshot_ts {op} ?
        ORDER BY snapshot_ts {order}
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    if not row or tolerance_seconds is None:
        return row
    snap_ts = _parse_ts(row["snapshot_ts"])
    if not snap_ts or abs((snap_ts - target).total_seconds()) > tolerance_seconds:
        return None
    return row


def _mid(row: sqlite3.Row | None) -> float | None:
    if row is None:
        return None
    value = row["yes_mid"]
    if value is not None:
        return float(value)
    bid = row["yes_bid"]
    ask = row["yes_ask"]
    if bid is not None and ask is not None:
        return (float(bid) + float(ask)) / 2
    return None


def _snapshot_at_or_after(conn: sqlite3.Connection, *, token_id: str | None, event_dt: datetime | None, seconds: int) -> sqlite3.Row | None:
    if not token_id or not event_dt:
        return None
    return _nearest_snapshot(conn, token_id=token_id, target_ts=(event_dt + timedelta(seconds=seconds)).isoformat(), before=False)


def _tags(*, role: str, phase: str, fill_vs_mid: float | None, markout_15s: float | None, pre_snapshot: sqlite3.Row | None) -> list[str]:
    tags: list[str] = []
    if pre_snapshot is None:
        tags.append("unknown_snapshot")
    if role == "maker" and (fill_vs_mid is not None and fill_vs_mid <= 0):
        tags.append("good_passive_fill")
    if role == "taker":
        tags.append("crossed_spread")
    if role == "inside_spread":
        tags.append("inside_spread_fill")
    if phase == "open":
        tags.append("opening_chaos_fill")
    if markout_15s is not None and markout_15s >= 0.03:
        tags.append("volatility_reversion_fill")
    return tags


def _event_rows(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    market_slug: str,
    since: str | None,
    asset: str | None,
    interval: str | None,
) -> list[sqlite3.Row]:
    clauses = ["LOWER(wallet_address)=LOWER(?)", "market_slug=?"]
    params: list[Any] = [wallet, market_slug]
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
    return conn.execute(
        f"""
        SELECT * FROM wallet_events
        WHERE {' AND '.join(clauses)}
        ORDER BY event_ts ASC, id ASC
        """,
        tuple(params),
    ).fetchall()


def _upsert_quality_event(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    cols = list(row.keys())
    assignments = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "event_id")
    conn.execute(
        f"""
        INSERT INTO execution_quality_events({','.join(cols)})
        VALUES({','.join(['?'] * len(cols))})
        ON CONFLICT(event_id) DO UPDATE SET {assignments}
        """,
        tuple(row[c] for c in cols),
    )


def _analyze_event(conn: sqlite3.Connection, event: sqlite3.Row, *, snapshot_tolerance_seconds: float, price_tolerance: float) -> dict[str, Any]:
    event_dt = _parse_ts(event["event_ts"])
    opened, window_seconds = _market_open(event["market_slug"], event["event_ts"])
    seconds_after_open = (event_dt - opened).total_seconds() if event_dt and opened else None
    phase = _phase(seconds_after_open, window_seconds)
    pre = _nearest_snapshot(conn, token_id=event["token_id"], target_ts=event["event_ts"], before=None, tolerance_seconds=snapshot_tolerance_seconds)
    post5 = _snapshot_at_or_after(conn, token_id=event["token_id"], event_dt=event_dt, seconds=5)
    post15 = _snapshot_at_or_after(conn, token_id=event["token_id"], event_dt=event_dt, seconds=15)
    post60 = _snapshot_at_or_after(conn, token_id=event["token_id"], event_dt=event_dt, seconds=60)

    fill = float(event["price"]) if event["price"] is not None else None
    pre_bid = float(pre["yes_bid"]) if pre is not None and pre["yes_bid"] is not None else None
    pre_ask = float(pre["yes_ask"]) if pre is not None and pre["yes_ask"] is not None else None
    pre_mid = _mid(pre)
    pre_spread = (pre_ask - pre_bid) if pre_bid is not None and pre_ask is not None else None
    role = classify_liquidity_role(event["action"], fill, pre_bid, pre_ask, tolerance=price_tolerance)
    direction = _direction(event["action"])
    post_mid_5 = _mid(post5)
    post_mid_15 = _mid(post15)
    post_mid_60 = _mid(post60)
    fill_vs_mid = (fill - pre_mid) if fill is not None and pre_mid is not None else None
    markout_5 = direction * (post_mid_5 - fill) if fill is not None and post_mid_5 is not None else None
    markout_15 = direction * (post_mid_15 - fill) if fill is not None and post_mid_15 is not None else None
    markout_60 = direction * (post_mid_60 - fill) if fill is not None and post_mid_60 is not None else None
    fill_vs_bid = fill - pre_bid if fill is not None and pre_bid is not None else None
    fill_vs_ask = fill - pre_ask if fill is not None and pre_ask is not None else None
    effective = None
    if fill_vs_mid is not None and pre_spread is not None and pre_spread > 0:
        effective = -direction * fill_vs_mid / (pre_spread / 2)
    tags = _tags(role=role, phase=phase, fill_vs_mid=fill_vs_mid, markout_15s=markout_15, pre_snapshot=pre)
    row = {
        "event_id": int(event["id"]),
        "wallet_address": event["wallet_address"],
        "market_slug": event["market_slug"],
        "market_id": event["market_id"],
        "token_id": event["token_id"],
        "side": event["side"],
        "action": event["action"],
        "fill_price": _round(fill),
        "size": _round(float(event["size"] or 0)),
        "notional": _round(float(event["notional"] or 0)),
        "event_ts": event["event_ts"],
        "pre_snapshot_ts": pre["snapshot_ts"] if pre is not None else None,
        "post_snapshot_ts": post5["snapshot_ts"] if post5 is not None else None,
        "pre_best_bid": _round(pre_bid),
        "pre_best_ask": _round(pre_ask),
        "pre_mid": _round(pre_mid),
        "pre_spread": _round(pre_spread),
        "post_mid_5s": _round(post_mid_5),
        "post_mid_15s": _round(post_mid_15),
        "post_mid_60s": _round(post_mid_60),
        "fill_vs_bid": _round(fill_vs_bid),
        "fill_vs_ask": _round(fill_vs_ask),
        "fill_vs_mid": _round(fill_vs_mid),
        "effective_spread_captured": _round(effective),
        "markout_5s": _round(markout_5),
        "markout_15s": _round(markout_15),
        "markout_60s": _round(markout_60),
        "seconds_after_open": _round(seconds_after_open),
        "phase": phase,
        "likely_liquidity_role": role,
        "fill_quality_tags": tags,
        "analysis_version": ANALYSIS_VERSION,
        "trade_id": event["trade_id"],
    }
    db_row = dict(row)
    db_row["fill_quality_tags"] = json.dumps(tags)
    db_row.pop("trade_id", None)
    _upsert_quality_event(conn, db_row)
    return row


def _avg(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [float(r[key]) for r in rows if r.get(key) is not None]
    return _round(mean(values)) if values else None


def _phase_summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for phase in PHASES:
        subset = [r for r in rows if r.get("phase") == phase]
        if subset:
            out[phase] = {"count": len(subset), "avg_markout_5s": _avg(subset, "markout_5s"), "avg_markout_15s": _avg(subset, "markout_15s"), "avg_markout_60s": _avg(subset, "markout_60s")}
    return out


def analyze_execution_quality(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    market_slug: str,
    since: str | None = None,
    asset: str | None = None,
    interval: str | None = None,
    snapshot_tolerance_seconds: float = 2.0,
    price_tolerance: float = 0.001,
) -> dict[str, Any]:
    rows = _event_rows(conn, wallet=wallet, market_slug=market_slug, since=since, asset=asset, interval=interval)
    events = [_analyze_event(conn, row, snapshot_tolerance_seconds=snapshot_tolerance_seconds, price_tolerance=price_tolerance) for row in rows]
    conn.commit()
    role_counts = {role: sum(1 for e in events if e["likely_liquidity_role"] == role) for role in ROLES}
    markable = [e for e in events if e.get("markout_5s") is not None]
    best = sorted(markable, key=lambda e: e.get("markout_5s") or -999, reverse=True)[:5]
    worst = sorted(markable, key=lambda e: e.get("markout_5s") or 999)[:5]
    return {
        "wallet_address": wallet.lower(),
        "market_slug": market_slug,
        "since": since,
        "asset": asset,
        "interval": interval,
        "snapshot_tolerance_seconds": snapshot_tolerance_seconds,
        "event_count": len(events),
        "role_counts": role_counts,
        "avg_fill_vs_mid": _avg(events, "fill_vs_mid"),
        "avg_markout_5s": _avg(events, "markout_5s"),
        "avg_markout_15s": _avg(events, "markout_15s"),
        "avg_markout_60s": _avg(events, "markout_60s"),
        "best_fills_by_markout": best,
        "worst_fills_by_markout": worst,
        "by_phase": _phase_summary(events),
        "events": events,
        "analysis_version": ANALYSIS_VERSION,
        "safety": "READ_ONLY_RESEARCH: no private keys, no signers, no orders, no automated trading.",
    }


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _fill_line(e: dict[str, Any]) -> str:
    return (
        f"{e.get('event_ts')} {e.get('side')} {e.get('action')} px={_fmt(e.get('fill_price'))} "
        f"role={e.get('likely_liquidity_role')} fvmid={_fmt(e.get('fill_vs_mid'))} "
        f"m5={_fmt(e.get('markout_5s'))} m15={_fmt(e.get('markout_15s'))} m60={_fmt(e.get('markout_60s'))} "
        f"phase={e.get('phase')} tags={','.join(e.get('fill_quality_tags') or [])} trade_id={e.get('trade_id') or '-'}"
    )


def format_execution_quality_report(summary: dict[str, Any]) -> str:
    roles = summary["role_counts"]
    by_phase = summary.get("by_phase", {})
    open_count = by_phase.get("open", {}).get("count", 0)
    good_open = any("good_passive_fill" in (e.get("fill_quality_tags") or []) for e in summary.get("events", []) if e.get("phase") == "open")
    volatility_count = sum(1 for e in summary.get("events", []) if "volatility_reversion_fill" in (e.get("fill_quality_tags") or []))
    lines = [
        "Polymarket Execution Quality — READ-ONLY RESEARCH",
        "Safety: no private keys, no signers, no order placement, no automated trading.",
        "Important caveat: maker/taker/inside is unknown unless a nearby orderbook snapshot exists within tolerance.",
        f"Wallet: {summary['wallet_address']}",
        f"Market: {summary['market_slug']}",
        f"Filters: since={summary.get('since') or '-'} asset={summary.get('asset') or '-'} interval={summary.get('interval') or '-'} snapshot_tolerance_seconds={summary.get('snapshot_tolerance_seconds')}",
        f"Events analyzed: {summary['event_count']}",
        "",
        "Role distribution:",
        f"  maker={roles.get('maker', 0)} taker={roles.get('taker', 0)} inside_spread={roles.get('inside_spread', 0)} unknown={roles.get('unknown', 0)}",
        "",
        "Average execution/markout:",
        f"  avg_fill_vs_mid={_fmt(summary.get('avg_fill_vs_mid'))}",
        f"  avg_markout_5s={_fmt(summary.get('avg_markout_5s'))}",
        f"  avg_markout_15s={_fmt(summary.get('avg_markout_15s'))}",
        f"  avg_markout_60s={_fmt(summary.get('avg_markout_60s'))}",
        "",
        "By phase:",
    ]
    if by_phase:
        for phase, data in by_phase.items():
            lines.append(f"  {phase}: count={data.get('count')} m5={_fmt(data.get('avg_markout_5s'))} m15={_fmt(data.get('avg_markout_15s'))} m60={_fmt(data.get('avg_markout_60s'))}")
    else:
        lines.append("  No phase data.")
    lines += ["", "Best fills by 5s markout:"]
    lines += ["  " + _fill_line(e) for e in summary.get("best_fills_by_markout", [])] or ["  -"]
    lines += ["", "Worst fills by 5s markout:"]
    lines += ["  " + _fill_line(e) for e in summary.get("worst_fills_by_markout", [])] or ["  -"]
    lines += ["", "Interpretation:"]
    if open_count and good_open:
        lines.append("  good fills cluster near open: YES — open-phase passive/cheap fills were detected.")
    elif open_count:
        lines.append("  good fills cluster near open: inconclusive — open fills exist, but passive positive markout evidence is weak.")
    else:
        lines.append("  good fills cluster near open: NO — no open-phase events in this filter set.")
    if volatility_count:
        lines.append(f"  volatility reversion fills: YES — {volatility_count} fills had positive 15s markout >= 3c.")
    else:
        lines.append("  volatility reversion fills: not detected by the current 15s markout threshold.")
    lines += ["", "Per-fill analysis:"]
    lines += ["  " + _fill_line(e) for e in summary.get("events", [])] or ["  No matching wallet events."]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze execution quality around Polymarket wallet fills using local orderbook snapshots.")
    parser.add_argument("--config", default="config.example.yaml")
    parser.add_argument("--wallet", required=True)
    parser.add_argument("--market", required=True, help="market_slug, e.g. btc-updown-5m-1779129000")
    parser.add_argument("--since", default=None)
    parser.add_argument("--asset", default=None)
    parser.add_argument("--interval", default=None)
    parser.add_argument("--snapshot-tolerance-seconds", type=float, default=2.0)
    args = parser.parse_args()
    cfg = load_config(args.config)
    conn = connect(cfg.db_path)
    initialize_schema(conn)
    summary = analyze_execution_quality(
        conn,
        wallet=args.wallet,
        market_slug=args.market,
        since=args.since,
        asset=args.asset,
        interval=args.interval,
        snapshot_tolerance_seconds=args.snapshot_tolerance_seconds,
    )
    print(format_execution_quality_report(summary))


if __name__ == "__main__":
    main()
