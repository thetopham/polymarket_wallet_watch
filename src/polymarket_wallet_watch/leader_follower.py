from __future__ import annotations

import argparse
import csv
import sqlite3
from collections import defaultdict
from statistics import mean, median

from .config import load_config
from .db import connect, initialize_schema
from .util import parse_ts


def compute_leader_follower_edges(events: list[dict], max_lag_seconds: int = 60) -> list[dict]:
    by_market_side = defaultdict(list)
    for e in events:
        by_market_side[(e.get("market_id"), e.get("token_id"), e.get("side"))].append(e)
    pairs = defaultdict(list)
    for group in by_market_side.values():
        group = sorted(group, key=lambda e: parse_ts(e.get("event_ts")))
        for i, a in enumerate(group):
            ta = parse_ts(a.get("event_ts"))
            if not ta:
                continue
            for b in group[i + 1:]:
                tb = parse_ts(b.get("event_ts"))
                if not tb:
                    continue
                lag = (tb - ta).total_seconds()
                if lag > max_lag_seconds:
                    break
                if a.get("wallet_address") != b.get("wallet_address"):
                    pairs[(a["wallet_address"], b["wallet_address"])].append(lag)
    rows = []
    for (leader, follower), lags in pairs.items():
        rows.append({
            "leader_wallet": leader,
            "follower_wallet": follower,
            "market_scope": "all",
            "pair_count": len(lags),
            "median_lag_seconds": median(lags),
            "avg_favorable_move_after_leader": None,
            "influence_score": len(lags) / max(mean(lags), 1.0),
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Export leader/follower edge CSV from wallet event ordering.")
    parser.add_argument("--config", default="config.example.yaml")
    parser.add_argument("--output", default="leader_follower_edges.csv")
    args = parser.parse_args()
    cfg = load_config(args.config)
    conn = connect(cfg.db_path)
    initialize_schema(conn)
    events = [dict(r) for r in conn.execute("SELECT * FROM wallet_events ORDER BY event_ts")]
    rows = compute_leader_follower_edges(events)
    with open(args.output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["leader_wallet", "follower_wallet", "market_scope", "pair_count", "median_lag_seconds", "avg_favorable_move_after_leader", "influence_score"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote_edges={len(rows)} path={args.output}")


if __name__ == "__main__":
    main()
