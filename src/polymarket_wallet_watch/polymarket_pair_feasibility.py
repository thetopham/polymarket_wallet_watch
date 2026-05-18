from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapter_polymarket_1s import DEFAULT_POLYMARKET_1S_DB, connect_feed, load_snapshots


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


def _phase(snapshot: dict[str, Any]) -> str:
    open_ts = _parse_ts(snapshot.get("market_open_time"))
    close_ts = _parse_ts(snapshot.get("market_close_time"))
    ts = _parse_ts(snapshot.get("ts"))
    if not ts or not open_ts or not close_ts:
        stc = snapshot.get("seconds_to_close")
        if stc is not None and stc <= 60:
            return "late"
        return "unknown"
    window = max((close_ts - open_ts).total_seconds(), 1)
    elapsed = (ts - open_ts).total_seconds()
    if elapsed <= 60:
        return "open"
    if elapsed >= max(window - 60, window * 0.8):
        return "late"
    return "mid"


def _depth(snapshot: dict[str, Any]) -> float | None:
    vals = [snapshot.get("yes_ask_depth"), snapshot.get("no_ask_depth")]
    vals = [float(v) for v in vals if v is not None]
    return min(vals) if vals else None


def _sizing_feasible(pair_cost: float | None, depth: float | None, sizing_checks_usd: list[float]) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for dollars in sizing_checks_usd:
        key = str(int(dollars)) if float(dollars).is_integer() else str(dollars)
        required_contracts = dollars / pair_cost if pair_cost and pair_cost > 0 else float("inf")
        out[key] = bool(depth is not None and depth >= required_contracts)
    return out


def scan_pair_feasibility(
    conn: sqlite3.Connection,
    *,
    target_pair_cost: float = 0.95,
    sizing_checks_usd: list[float] | None = None,
    market_key: str | None = None,
    since: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    checks = sizing_checks_usd or [50, 200, 1000]
    snapshots = load_snapshots(conn, market_key=market_key, since=since, limit=limit)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for snap in snapshots:
        key = snap.get("market_key") or snap.get("market_slug") or "unknown"
        grouped.setdefault(key, []).append(snap)
    contracts: list[dict[str, Any]] = []
    for key, rows in grouped.items():
        enriched = []
        for snap in rows:
            pair_cost = snap.get("pair_ask_cost")
            depth = _depth(snap)
            enriched.append({**snap, "phase": _phase(snap), "visible_pair_depth": depth})
        with_cost = [s for s in enriched if s.get("pair_ask_cost") is not None]
        if not with_cost:
            continue
        best = min(with_cost, key=lambda s: (s.get("pair_ask_cost"), s.get("ts")))
        opportunities = [s for s in with_cost if s.get("pair_ask_cost") is not None and s["pair_ask_cost"] <= target_pair_cost]
        best_depth = max((_depth(s) or 0 for s in with_cost), default=0.0)
        best_passive_credit = max((s.get("pair_bid_credit") for s in with_cost if s.get("pair_bid_credit") is not None), default=None)
        phase_counts: dict[str, int] = {}
        for opp in opportunities:
            phase_counts[opp["phase"]] = phase_counts.get(opp["phase"], 0) + 1
        contracts.append({
            "venue": "polymarket",
            "market_key": key,
            "best_pair_cost": round(float(best["pair_ask_cost"]), 6),
            "best_pair_cost_ts": best.get("ts"),
            "best_pair_cost_phase": best.get("phase"),
            "best_visible_pair_depth": round(float(_depth(best) or 0), 6),
            "max_visible_pair_depth": round(float(best_depth), 6),
            "best_pair_bid_credit": round(float(best_passive_credit), 6) if best_passive_credit is not None else None,
            "sizing_feasible": _sizing_feasible(best.get("pair_ask_cost"), _depth(best), checks),
            "opportunity_count": len(opportunities),
            "phase_opportunity_counts": phase_counts,
            "snapshot_count": len(rows),
            "target_pair_cost": target_pair_cost,
        })
    contracts.sort(key=lambda c: (c["best_pair_cost"], -c["opportunity_count"]))
    return {"venue": "polymarket", "target_pair_cost": target_pair_cost, "sizing_checks_usd": checks, "contract_count": len(contracts), "contracts": contracts}


def format_pair_feasibility_report(result: dict[str, Any]) -> str:
    lines = [
        "Polymarket 1s Pair Feasibility — READ-ONLY",
        "Safety: reads local Polymarket 1s SQLite snapshots only; no private keys, no signers, no orders.",
        f"Contracts: {result.get('contract_count', 0)} target_pair_cost={result.get('target_pair_cost')}",
        "",
    ]
    if not result.get("contracts"):
        lines.append("No Polymarket 1s pair feasibility rows found. Check feed/polymarket-btc-1s.sqlite3 freshness/path.")
        return "\n".join(lines)
    for c in result["contracts"]:
        sizing = c.get("sizing_feasible", {})
        lines.extend([
            f"Contract: {c['market_key']}",
            f"- best pair cost: {c['best_pair_cost']} at {c['best_pair_cost_ts']} phase={c['best_pair_cost_phase']}",
            f"- visible depth at best: {c['best_visible_pair_depth']} max_visible_depth={c['max_visible_pair_depth']}",
            f"- passive bid pair credit: {c.get('best_pair_bid_credit')}",
            f"- opportunities <= target: {c['opportunity_count']} by phase {c.get('phase_opportunity_counts', {})}",
            f"- $50 feasible: {sizing.get('50', False)} | $200 feasible: {sizing.get('200', False)} | $1000 feasible: {sizing.get('1000', False)}",
            "",
        ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan Polymarket 1s BTC feed for YES+NO pair-cost feasibility.")
    parser.add_argument("--feed-db", default=str(DEFAULT_POLYMARKET_1S_DB))
    parser.add_argument("--market")
    parser.add_argument("--since")
    parser.add_argument("--target-pair-cost", type=float, default=0.95)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    conn = connect_feed(args.feed_db)
    print(format_pair_feasibility_report(scan_pair_feasibility(conn, target_pair_cost=args.target_pair_cost, market_key=args.market, since=args.since, limit=args.limit)))


if __name__ == "__main__":
    main()
