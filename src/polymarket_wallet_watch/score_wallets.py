from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter, defaultdict
from statistics import mean, pstdev
from typing import Any, Iterable

from .config import load_config
from .db import connect, initialize_schema


def _edge_for_event(event: dict[str, Any], key: str) -> float | None:
    price = event.get("price")
    mark = event.get(key)
    if price is None or mark is None:
        return None
    # A NO token mark is already the NO token price in normalized alpha inputs.
    return float(mark) - float(price)


def _avg(values: list[float]) -> float | None:
    return mean(values) if values else None


def _win_rate(values: list[float]) -> float | None:
    return sum(1 for v in values if v > 0) / len(values) if values else None


def _sharpe(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    sd = pstdev(values)
    return (mean(values) / sd) * math.sqrt(len(values)) if sd else None


def compute_wallet_alpha_rows(events: Iterable[dict[str, Any]], *, asof_ts: str = "latest") -> list[dict[str, Any]]:
    by_wallet: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_wallet[str(event["wallet_address"]).lower()].append(event)
    mm_flags = flag_likely_market_makers(list(events)) if not isinstance(events, list) else flag_likely_market_makers(events)
    rows = []
    for wallet, wallet_events in by_wallet.items():
        e15 = [_edge_for_event(e, "mark_15s") for e in wallet_events]
        e30 = [_edge_for_event(e, "mark_30s") for e in wallet_events]
        e60 = [_edge_for_event(e, "mark_60s") for e in wallet_events]
        e180 = [_edge_for_event(e, "mark_180s") for e in wallet_events]
        edge_lists = [[x for x in xs if x is not None] for xs in (e15, e30, e60, e180)]
        all_edges = [x for xs in edge_lists for x in xs]
        expiry_edges = [float(e["expiry_price"]) - float(e["price"]) for e in wallet_events if e.get("expiry_price") is not None and e.get("price") is not None]
        rows.append({
            "wallet_address": wallet,
            "asof_ts": asof_ts,
            "event_count": len(wallet_events),
            "avg_edge_15s": _avg(edge_lists[0]),
            "avg_edge_30s": _avg(edge_lists[1]),
            "avg_edge_60s": _avg(edge_lists[2]),
            "avg_edge_180s": _avg(edge_lists[3]),
            "win_rate_15s": _win_rate(edge_lists[0]),
            "win_rate_30s": _win_rate(edge_lists[1]),
            "win_rate_60s": _win_rate(edge_lists[2]),
            "win_rate_180s": _win_rate(edge_lists[3]),
            "max_favorable_excursion": max(all_edges) if all_edges else None,
            "max_adverse_excursion": min(all_edges) if all_edges else None,
            "expiry_pnl": sum(expiry_edges) if expiry_edges else None,
            "expiry_win_rate": _win_rate(expiry_edges),
            "sharpe_like_score": _sharpe(edge_lists[2] or all_edges),
            "consistency_by_regime": json.dumps({}),
            "likely_market_maker": int(mm_flags.get(wallet, False)),
        })
    return rows


def flag_likely_market_makers(events: Iterable[dict[str, Any]], *, min_events: int = 50, both_side_ratio: float = 0.35, sell_ratio: float = 0.25) -> dict[str, bool]:
    by_wallet: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_wallet[str(event["wallet_address"]).lower()].append(event)
    flags = {}
    for wallet, wallet_events in by_wallet.items():
        n = len(wallet_events)
        sides = Counter(e.get("side") for e in wallet_events)
        actions = Counter(e.get("action") for e in wallet_events)
        yes_ratio = sides.get("YES", 0) / n if n else 0
        no_ratio = sides.get("NO", 0) / n if n else 0
        sells = actions.get("sell", 0) / n if n else 0
        flags[wallet] = n >= min_events and yes_ratio >= both_side_ratio and no_ratio >= both_side_ratio and sells >= sell_ratio
    return flags


def load_markout_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    # If explicit markouts have not been backfilled yet, use enriched/orderbook mids as placeholders.
    rows = []
    for row in conn.execute(
        """
        SELECT we.wallet_address, we.side, we.price, we.event_ts,
               e.yes_mid AS mark_15s, e.yes_mid AS mark_30s, e.yes_mid AS mark_60s, e.yes_mid AS mark_180s
        FROM wallet_events we
        LEFT JOIN enriched_wallet_events e ON e.event_id = we.id
        WHERE we.price IS NOT NULL
        """
    ):
        rows.append(dict(row))
    return rows


def persist_alpha_rows(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    cols = list(rows[0].keys()) if rows else []
    if not cols:
        return
    placeholders = ",".join(["?"] * len(cols))
    for row in rows:
        conn.execute(f"INSERT OR REPLACE INTO wallet_alpha({','.join(cols)}) VALUES({placeholders})", tuple(row[c] for c in cols))
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Score watched wallets by post-event alpha markouts.")
    parser.add_argument("--config", default="config.example.yaml")
    args = parser.parse_args()
    cfg = load_config(args.config)
    conn = connect(cfg.db_path)
    initialize_schema(conn)
    rows = compute_wallet_alpha_rows(load_markout_events(conn))
    persist_alpha_rows(conn, rows)
    print(f"wallet_alpha_rows={len(rows)}")


if __name__ == "__main__":
    main()
