from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from statistics import mean
from typing import Any

from .config import load_config
from .db import connect, initialize_schema
from .util import parse_ts


def direction_for_event(event: dict[str, Any]) -> int:
    action = str(event.get("action") or "").lower()
    side = str(event.get("side") or "").upper()
    base = 1 if side == "YES" else -1 if side == "NO" else 0
    if action in {"sell", "reduce", "exit"}:
        base *= -1
    return base


def compute_consensus_score(
    events: list[dict[str, Any]],
    wallet_alpha: dict[str, float],
    *,
    cluster_end_ts: str,
    half_life_seconds: float = 30,
) -> float:
    end = parse_ts(cluster_end_ts)
    score = 0.0
    for event in events:
        wallet = str(event.get("wallet_address", "")).lower()
        alpha_weight = float(wallet_alpha.get(wallet, 1.0))
        direction = direction_for_event(event)
        size = float(event.get("size") or event.get("notional") or 1.0)
        size_weight = math.sqrt(max(size, 0.0))
        event_ts = parse_ts(event.get("event_ts"))
        lag = max((end - event_ts).total_seconds(), 0.0) if end and event_ts else 0.0
        recency_weight = 0.5 ** (lag / half_life_seconds) if half_life_seconds > 0 else 1.0
        score += alpha_weight * direction * size_weight * recency_weight
    return score


def build_convergence_clusters(
    events: list[dict[str, Any]],
    wallet_alpha: dict[str, float],
    *,
    window_seconds: int = 30,
    min_wallets: int = 2,
) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        key = (str(event.get("market_id") or event.get("condition_id") or ""), str(event.get("token_id") or ""), str(event.get("side") or "UNKNOWN"))
        by_key[key].append(event)

    clusters: list[dict[str, Any]] = []
    seen_event_ids: set[tuple[Any, ...]] = set()
    for (market_id, token_id, side), group in by_key.items():
        group = sorted(group, key=lambda e: parse_ts(e.get("event_ts")) or parse_ts("1970-01-01T00:00:00Z"))
        for i, first in enumerate(group):
            start = parse_ts(first.get("event_ts"))
            if not start:
                continue
            window_events = []
            wallets = set()
            for event in group[i:]:
                ts = parse_ts(event.get("event_ts"))
                if not ts:
                    continue
                if (ts - start).total_seconds() <= window_seconds:
                    window_events.append(event)
                    wallets.add(str(event.get("wallet_address", "")).lower())
                else:
                    break
            if len(wallets) < min_wallets:
                continue
            event_key = tuple(e.get("id") or (e.get("wallet_address"), e.get("event_ts")) for e in window_events)
            if event_key in seen_event_ids:
                continue
            seen_event_ids.add(event_key)
            end_ts = max(parse_ts(e.get("event_ts")) for e in window_events if parse_ts(e.get("event_ts")))
            start_ts = min(parse_ts(e.get("event_ts")) for e in window_events if parse_ts(e.get("event_ts")))
            leader = min(window_events, key=lambda e: parse_ts(e.get("event_ts")) or end_ts)
            leader_ts = parse_ts(leader.get("event_ts")) or start_ts
            lags = [float((parse_ts(e.get("event_ts")) - leader_ts).total_seconds()) for e in window_events if e is not leader and parse_ts(e.get("event_ts"))]
            directions = [direction_for_event(e) for e in window_events]
            majority = 1 if sum(1 for d in directions if d >= 0) >= sum(1 for d in directions if d < 0) else -1
            directional_agreement = sum(1 for d in directions if d == majority) / len(directions)
            alphas = [float(wallet_alpha.get(str(e.get("wallet_address", "")).lower(), 1.0)) for e in window_events]
            clusters.append({
                "cluster_start_ts": start_ts.isoformat(),
                "cluster_end_ts": end_ts.isoformat(),
                "window_seconds": window_seconds,
                "market_id": market_id,
                "condition_id": window_events[0].get("condition_id"),
                "token_id": token_id,
                "side": side,
                "wallet_count": len(wallets),
                "wallet_addresses": sorted(wallets),
                "consensus_score": compute_consensus_score(window_events, wallet_alpha, cluster_end_ts=end_ts.isoformat()),
                "total_notional": sum(float(e.get("notional") or 0) for e in window_events),
                "avg_wallet_alpha": mean(alphas) if alphas else None,
                "leader_wallet": str(leader.get("wallet_address", "")).lower(),
                "follower_lags_seconds": lags,
                "directional_agreement": directional_agreement,
                "raw_event_ids": [e.get("id") for e in window_events],
            })
    return clusters


def load_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute("SELECT * FROM wallet_events ORDER BY event_ts")]


def load_wallet_alpha(conn: sqlite3.Connection) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT wa.wallet_address, wa.sharpe_like_score, wa.avg_edge_60s
        FROM wallet_alpha wa
        JOIN (SELECT wallet_address, max(asof_ts) as asof_ts FROM wallet_alpha GROUP BY wallet_address) latest
          ON latest.wallet_address=wa.wallet_address AND latest.asof_ts=wa.asof_ts
        """
    ).fetchall()
    return {r["wallet_address"].lower(): float(r["sharpe_like_score"] if r["sharpe_like_score"] is not None else (r["avg_edge_60s"] or 1.0)) for r in rows}


def persist_clusters(conn: sqlite3.Connection, clusters: list[dict[str, Any]]) -> None:
    if not clusters:
        return
    for cluster in clusters:
        row = dict(cluster)
        row["wallet_addresses"] = json.dumps(row["wallet_addresses"])
        row["follower_lags_seconds"] = json.dumps(row["follower_lags_seconds"])
        row["raw_event_ids"] = json.dumps(row["raw_event_ids"])
        cols = list(row.keys())
        conn.execute(f"INSERT INTO convergence_clusters({','.join(cols)}) VALUES({','.join(['?']*len(cols))})", tuple(row[c] for c in cols))
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect same-market wallet convergence clusters.")
    parser.add_argument("--config", default="config.example.yaml")
    parser.add_argument("--window", type=int, default=30)
    parser.add_argument("--min-wallets", type=int, default=2)
    args = parser.parse_args()
    cfg = load_config(args.config)
    conn = connect(cfg.db_path)
    initialize_schema(conn)
    clusters = build_convergence_clusters(load_events(conn), load_wallet_alpha(conn), window_seconds=args.window, min_wallets=args.min_wallets)
    persist_clusters(conn, clusters)
    print(f"convergence_clusters={len(clusters)} window={args.window}")


if __name__ == "__main__":
    main()
