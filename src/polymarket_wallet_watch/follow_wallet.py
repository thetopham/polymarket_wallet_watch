from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import load_config
from .db import connect, initialize_schema

ADDRESS_RE = re.compile(r"^0x[a-f0-9]{40}$")


def normalize_wallet_address(address: str) -> str:
    normalized = str(address).strip().lower()
    if not ADDRESS_RE.match(normalized):
        raise ValueError(f"invalid wallet address: {address}")
    return normalized


def _since_clause(since: str | None) -> tuple[str, tuple[Any, ...]]:
    if not since:
        return "", ()
    text = since.strip().lower()
    if text.endswith("h") and text[:-1].isdigit():
        cutoff = datetime.now(timezone.utc) - timedelta(hours=int(text[:-1]))
        return "AND event_ts >= ?", (cutoff.isoformat(),)
    return "AND event_ts >= ?", (since,)


def format_wallet_follow_report(
    conn: sqlite3.Connection,
    wallet_address: str,
    *,
    limit: int = 100,
    since: str | None = None,
) -> str:
    wallet = normalize_wallet_address(wallet_address)
    since_sql, params = _since_clause(since)
    rows = conn.execute(
        f"""
        SELECT wallet_address, event_ts, market_id, condition_id, token_id, market_slug, market_title,
               side, action, price, size, notional, seconds_to_close, source, tx_hash, trade_id
        FROM wallet_events
        WHERE wallet_address = ?
        {since_sql}
        ORDER BY event_ts DESC
        LIMIT ?
        """,
        (wallet, *params, limit),
    ).fetchall()
    alpha = conn.execute(
        """
        SELECT event_count, avg_edge_15s, avg_edge_30s, avg_edge_60s, avg_edge_180s,
               win_rate_60s, sharpe_like_score, likely_market_maker
        FROM wallet_alpha
        WHERE wallet_address = ?
        ORDER BY asof_ts DESC
        LIMIT 1
        """,
        (wallet,),
    ).fetchone()
    lines = [
        "Individual Wallet Follow — READ-ONLY RESEARCH",
        "Safety: no private keys, no orders, no automatic trading.",
        f"Wallet: {wallet}",
        f"Events shown: {len(rows)}",
        "",
    ]
    if alpha:
        mm = " likely_market_maker=YES" if alpha["likely_market_maker"] else ""
        lines.append(
            "Alpha summary: "
            f"events={alpha['event_count']} edge15={alpha['avg_edge_15s']} edge30={alpha['avg_edge_30s']} "
            f"edge60={alpha['avg_edge_60s']} edge180={alpha['avg_edge_180s']} "
            f"win60={alpha['win_rate_60s']} sharpe={alpha['sharpe_like_score']}{mm}"
        )
    else:
        lines.append("Alpha summary: unavailable until score_wallets has markouts for this wallet.")
    lines.append("")
    if not rows:
        lines.append("No events found for this wallet yet. Run ingest_wallets with this wallet enabled in config.yaml.")
        return "\n".join(lines)
    for r in rows:
        lines.append(
            f"{r['event_ts']} | {r['market_slug'] or r['market_title'] or r['market_id'] or '-'} | "
            f"{r['side']} {r['action']} | px={r['price']} size={r['size']} notional={r['notional']} | "
            f"ttc={r['seconds_to_close']}s | token={r['token_id']} | src={r['source']}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Follow one public Polymarket wallet in read-only mode.")
    parser.add_argument("--config", default="config.example.yaml")
    parser.add_argument("--wallet", default=None, help="Wallet address to follow. Defaults to follow_wallet.focus_wallet in config.")
    parser.add_argument("--since", default=None, help="e.g. 24h or ISO timestamp")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    cfg = load_config(args.config)
    wallet = args.wallet or cfg.data.get("follow_wallet", {}).get("focus_wallet")
    if not wallet:
        raise SystemExit("No wallet supplied. Use --wallet or set follow_wallet.focus_wallet in config.")
    conn = connect(cfg.db_path)
    initialize_schema(conn)
    print(format_wallet_follow_report(conn, wallet, limit=args.limit, since=args.since))


if __name__ == "__main__":
    main()
