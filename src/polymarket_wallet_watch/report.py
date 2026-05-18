from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import load_config
from .db import connect, initialize_schema, insert_dict


def insert_wallet_event(conn: sqlite3.Connection, event: dict[str, Any], *, or_ignore: bool = False) -> int:
    return insert_dict(conn, "wallet_events", event, or_ignore=or_ignore)


def _since_clause(since: str | None) -> tuple[str, tuple[Any, ...]]:
    if not since:
        return "", ()
    text = since.strip().lower()
    if text.endswith("h") and text[:-1].isdigit():
        cutoff = datetime.now(timezone.utc) - timedelta(hours=int(text[:-1]))
        return "WHERE event_ts >= ?", (cutoff.isoformat(),)
    return "WHERE event_ts >= ?", (since,)


def format_last_events_report(conn: sqlite3.Connection, *, limit: int = 100, since: str | None = None) -> str:
    where, params = _since_clause(since)
    rows = conn.execute(
        f"""
        SELECT wallet_address, event_ts, market_slug, market_title, side, action, price, size, notional,
               seconds_to_close, source, tx_hash, trade_id
        FROM wallet_events
        {where}
        ORDER BY event_ts DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    lines = [
        "Polymarket Wallet Watch — READ-ONLY RESEARCH",
        "Safety: no private keys, no live orders, no automatic trading.",
        f"Last normalized wallet events: {len(rows)}",
        "",
    ]
    if not rows:
        lines.append("No wallet events found. Run ingest_wallets after adding enabled wallets to config.")
        return "\n".join(lines)
    for r in rows:
        lines.append(
            f"{r['event_ts']} | {r['wallet_address']} | {r['market_slug'] or r['market_title'] or '-'} | "
            f"{r['side']} {r['action']} | px={r['price']} size={r['size']} notional={r['notional']} | "
            f"ttc={r['seconds_to_close']}s | src={r['source']}"
        )
    return "\n".join(lines)


def format_daily_report(conn: sqlite3.Connection, *, since: str = "24h") -> str:
    top_wallets = conn.execute(
        """
        SELECT wallet_address, event_count, avg_edge_60s, win_rate_60s, sharpe_like_score, likely_market_maker
        FROM wallet_alpha
        ORDER BY asof_ts DESC, COALESCE(sharpe_like_score, avg_edge_60s, 0) DESC
        LIMIT 10
        """
    ).fetchall()
    clusters = conn.execute(
        """
        SELECT cluster_start_ts, market_id, side, wallet_count, consensus_score, total_notional, leader_wallet,
               forward_markout_60s
        FROM convergence_clusters
        ORDER BY ABS(consensus_score) DESC
        LIMIT 10
        """
    ).fetchall()
    lines = [
        "Polymarket Wallet Watch Daily — READ-ONLY RESEARCH",
        "Warnings: latency, visible CLOB liquidity, external hedging, and wallet market-making can invalidate copy signals.",
        "",
        "Top wallets by recent alpha:",
    ]
    if top_wallets:
        for r in top_wallets:
            flag = " IGNORE/MM?" if r["likely_market_maker"] else ""
            lines.append(f"- {r['wallet_address']} events={r['event_count']} edge60={r['avg_edge_60s']} win60={r['win_rate_60s']} sharpe={r['sharpe_like_score']}{flag}")
    else:
        lines.append("- No wallet_alpha rows yet. Run score_wallets after markout/enrichment backfill.")
    lines += ["", "Strongest convergence clusters:"]
    if clusters:
        for r in clusters:
            lines.append(f"- {r['cluster_start_ts']} market={r['market_id']} side={r['side']} wallets={r['wallet_count']} score={r['consensus_score']:.3f} leader={r['leader_wallet']} fwd60={r['forward_markout_60s']}")
    else:
        lines.append("- No clusters yet. Run detect_convergence after wallet ingestion/scoring.")
    lines += ["", "Recent normalized events:", format_last_events_report(conn, limit=20, since=since)]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Print Polymarket wallet-convergence research reports.")
    parser.add_argument("--config", default="config.example.yaml")
    parser.add_argument("--since", default=None, help="e.g. 24h or ISO timestamp")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--daily", action="store_true")
    args = parser.parse_args()
    cfg = load_config(args.config)
    conn = connect(cfg.db_path)
    initialize_schema(conn)
    if args.daily:
        print(format_daily_report(conn, since=args.since or "24h"))
    else:
        print(format_last_events_report(conn, limit=args.limit, since=args.since))


if __name__ == "__main__":
    main()
