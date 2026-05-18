from __future__ import annotations

import argparse
import sqlite3
import time
from typing import Any

from .config import load_config
from .db import connect, initialize_schema, insert_dict
from .ingest_orderbooks import fetch_orderbook
from .util import parse_ts, to_float, utc_now_iso, write_raw_json


def _best_ask(book: dict[str, Any]) -> tuple[float | None, float | None]:
    asks = book.get("asks") or book.get("sell") or []
    parsed: list[tuple[float, float | None]] = []
    for level in asks:
        if not isinstance(level, dict):
            continue
        price = to_float(level.get("price"))
        size = to_float(level.get("size"))
        if price is not None:
            parsed.append((price, size))
    if not parsed:
        return None, None
    parsed.sort(key=lambda item: item[0])
    return parsed[0]


def _best_bid(book: dict[str, Any]) -> tuple[float | None, float | None]:
    bids = book.get("bids") or book.get("buy") or []
    parsed: list[tuple[float, float | None]] = []
    for level in bids:
        if not isinstance(level, dict):
            continue
        price = to_float(level.get("price"))
        size = to_float(level.get("size"))
        if price is not None:
            parsed.append((price, size))
    if not parsed:
        return None, None
    parsed.sort(key=lambda item: item[0], reverse=True)
    return parsed[0]


def compute_pair_cost_row(
    *,
    market_id: str,
    market_slug: str | None,
    open_ts: str,
    observed_ts: str,
    yes_book: dict[str, Any],
    no_book: dict[str, Any],
    btc_price: float | None = None,
    strike: float | None = None,
    realized_vol_60s: float | None = None,
    source: str = "clob_opening_window",
) -> dict[str, Any]:
    yes_ask, yes_liq = _best_ask(yes_book)
    no_ask, no_liq = _best_ask(no_book)
    yes_bid, _ = _best_bid(yes_book)
    no_bid, _ = _best_bid(no_book)
    pair_cost = round(yes_ask + no_ask, 10) if yes_ask is not None and no_ask is not None else None
    yes_spread = (yes_ask - yes_bid) if yes_ask is not None and yes_bid is not None else 0.0
    no_spread = (no_ask - no_bid) if no_ask is not None and no_bid is not None else 0.0
    spread_adjusted_pair_cost = round(pair_cost + yes_spread + no_spread, 10) if pair_cost is not None else None
    observed = parse_ts(observed_ts)
    opened = parse_ts(open_ts)
    seconds_after_open = (observed - opened).total_seconds() if observed and opened else None
    return {
        "market_id": market_id,
        "market_slug": market_slug,
        "open_ts": open_ts,
        "observed_ts": observed_ts,
        "seconds_after_open": seconds_after_open,
        "yes_best_ask": yes_ask,
        "no_best_ask": no_ask,
        "yes_liquidity": yes_liq,
        "no_liquidity": no_liq,
        "pair_cost": pair_cost,
        "spread_adjusted_pair_cost": spread_adjusted_pair_cost,
        "btc_price": btc_price,
        "strike": strike,
        "distance_from_strike": (btc_price - strike) if btc_price is not None and strike is not None else None,
        "realized_vol_60s": realized_vol_60s,
        "source": source,
    }


def insert_pair_cost_row(conn: sqlite3.Connection, row: dict[str, Any]) -> int:
    return insert_dict(conn, "opening_window_pair_costs", row, or_ignore=True)


def load_active_open_markets(conn: sqlite3.Connection, window_seconds: int) -> list[sqlite3.Row]:
    now = utc_now_iso()
    return conn.execute(
        """
        SELECT * FROM markets
        WHERE start_ts IS NOT NULL
          AND start_ts <= ?
          AND (close_ts IS NULL OR close_ts > ?)
          AND (strftime('%s', ?) - strftime('%s', start_ts)) BETWEEN 0 AND ?
        ORDER BY start_ts DESC
        """,
        (now, now, now, window_seconds),
    ).fetchall()


def infer_yes_no_token_ids(conn: sqlite3.Connection, market_id: str) -> tuple[str | None, str | None]:
    yes = conn.execute(
        "SELECT token_id FROM wallet_events WHERE market_id=? AND side='YES' AND token_id IS NOT NULL LIMIT 1",
        (market_id,),
    ).fetchone()
    no = conn.execute(
        "SELECT token_id FROM wallet_events WHERE market_id=? AND side='NO' AND token_id IS NOT NULL LIMIT 1",
        (market_id,),
    ).fetchone()
    return (yes[0] if yes else None, no[0] if no else None)


def collect_opening_window_once(conn: sqlite3.Connection, config: dict[str, Any], *, window_seconds: int) -> int:
    count = 0
    raw_dir = config.get("database", {}).get("raw_response_dir", "raw")
    for market in load_active_open_markets(conn, window_seconds):
        yes_token, no_token = infer_yes_no_token_ids(conn, market["market_id"])
        if not yes_token or not no_token:
            continue
        observed_ts = utc_now_iso()
        yes_book = fetch_orderbook(config, yes_token)
        no_book = fetch_orderbook(config, no_token)
        write_raw_json(raw_dir, f"opening-window-{market['market_id']}-yes", yes_book)
        write_raw_json(raw_dir, f"opening-window-{market['market_id']}-no", no_book)
        row = compute_pair_cost_row(
            market_id=market["market_id"],
            market_slug=market["slug"],
            open_ts=market["start_ts"],
            observed_ts=observed_ts,
            yes_book=yes_book,
            no_book=no_book,
            strike=market["strike"],
        )
        insert_pair_cost_row(conn, row)
        count += 1
    return count


def format_opening_window_report(conn: sqlite3.Connection, *, limit: int = 20) -> str:
    rows = conn.execute(
        """
        SELECT market_id, market_slug, open_ts, observed_ts, seconds_after_open, yes_best_ask, no_best_ask,
               yes_liquidity, no_liquidity, pair_cost, spread_adjusted_pair_cost, btc_price, strike,
               distance_from_strike, realized_vol_60s, source
        FROM opening_window_pair_costs
        ORDER BY pair_cost ASC, observed_ts DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    lines = [
        "Opening Window Pair-Cost Study — READ-ONLY RESEARCH",
        "Goal: measure whether first 15-120s after open offers better dual-side inventory opportunities.",
        "Safety: public CLOB reads only; no private keys, no orders.",
        f"Rows: {len(rows)}",
        "",
    ]
    if not rows:
        lines.append("No opening-window pair-cost rows yet. Run study_opening_window during an active market open window.")
        return "\n".join(lines)
    for r in rows:
        pair = f"{r['pair_cost']:.4f}" if r["pair_cost"] is not None else "None"
        spread_adj = f"{r['spread_adjusted_pair_cost']:.4f}" if r["spread_adjusted_pair_cost"] is not None else "None"
        lines.append(
            f"{r['observed_ts']} | market={r['market_id']} slug={r['market_slug']} "
            f"t+{r['seconds_after_open']}s | YES={r['yes_best_ask']} NO={r['no_best_ask']} "
            f"pair={pair} spread_adj={spread_adj} | liq_yes={r['yes_liquidity']} liq_no={r['no_liquidity']}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Study opening-window YES+NO pair costs from public CLOB books (read-only).")
    parser.add_argument("--config", default="config.example.yaml")
    parser.add_argument("--window-seconds", type=int, default=120)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--max-iterations", type=int, default=1, help="Bounded by default; increase for a 1 Hz open-window capture.")
    parser.add_argument("--report", action="store_true", help="Only print stored opening-window rows.")
    args = parser.parse_args()
    cfg = load_config(args.config)
    conn = connect(cfg.db_path)
    initialize_schema(conn)
    if args.report:
        print(format_opening_window_report(conn))
        return
    total = 0
    for i in range(args.max_iterations):
        total += collect_opening_window_once(conn, cfg.data, window_seconds=args.window_seconds)
        if i + 1 < args.max_iterations:
            time.sleep(args.poll_seconds)
    print(f"opening_window_pair_cost_rows={total}")
    print(format_opening_window_report(conn, limit=10))


if __name__ == "__main__":
    main()
