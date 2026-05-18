from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from statistics import mean

from .config import load_config
from .db import connect, initialize_schema
from .util import parse_ts


def split_clusters(clusters: list[dict], split_date: str) -> tuple[list[dict], list[dict]]:
    split = parse_ts(split_date)
    train, test = [], []
    for c in clusters:
        ts = parse_ts(c.get("cluster_start_ts"))
        (train if ts and ts < split else test).append(c)
    return train, test


def simple_replay(clusters: list[dict], threshold: float, slippage_bps: float = 50) -> dict:
    signals = [c for c in clusters if abs(float(c.get("consensus_score") or 0)) >= threshold]
    pnl_values = []
    for c in signals:
        mark = c.get("forward_markout_60s") or c.get("forward_markout_30s") or c.get("forward_markout_15s")
        if mark is not None:
            pnl_values.append(float(mark) - (slippage_bps / 10000.0))
    return {
        "signal_count": len(signals),
        "net_pnl": sum(pnl_values) if pnl_values else 0.0,
        "avg_edge_cents": mean(pnl_values) * 100 if pnl_values else 0.0,
        "win_rate": sum(1 for x in pnl_values if x > 0) / len(pnl_values) if pnl_values else None,
    }


def persist_result(conn: sqlite3.Connection, run_id: str, split_name: str, strategy_name: str, params: dict, result: dict) -> None:
    conn.execute(
        """
        INSERT INTO signal_replay_results(run_id, split_name, strategy_name, params_json, signal_count,
                                          simulated_entry_mode, conservative_slippage_bps, net_pnl,
                                          avg_edge_cents, win_rate)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (run_id, split_name, strategy_name, json.dumps(params, sort_keys=True), result["signal_count"], params.get("entry_mode"), params.get("slippage_bps"), result["net_pnl"], result["avg_edge_cents"], result["win_rate"]),
    )
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay convergence signals with chronological train/test split.")
    parser.add_argument("--config", default="config.example.yaml")
    parser.add_argument("--split-date", required=True)
    parser.add_argument("--threshold", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=50)
    args = parser.parse_args()
    cfg = load_config(args.config)
    conn = connect(cfg.db_path)
    initialize_schema(conn)
    clusters = [dict(r) for r in conn.execute("SELECT * FROM convergence_clusters ORDER BY cluster_start_ts")]
    train, test = split_clusters(clusters, args.split_date)
    params = {"threshold": args.threshold, "slippage_bps": args.slippage_bps, "entry_mode": "taker"}
    run_id = datetime.now(timezone.utc).strftime("replay-%Y%m%dT%H%M%SZ")
    for name, rows in [("train", train), ("test", test)]:
        result = simple_replay(rows, args.threshold, args.slippage_bps)
        persist_result(conn, run_id, name, "convergence_threshold_v0", params, result)
        print(f"{name}: {json.dumps(result, sort_keys=True)}")


if __name__ == "__main__":
    main()
