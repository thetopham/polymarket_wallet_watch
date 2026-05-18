from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_POLYMARKET_1S_DB = Path("/home/matt/workspace/kalshi-btc-15m-bot/feed/polymarket-btc-1s.sqlite3")


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_loads(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def connect_feed(path: str | Path = DEFAULT_POLYMARKET_1S_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def parse_orderbook_depth(book: dict[str, Any] | None) -> dict[str, float | None]:
    if not isinstance(book, dict):
        return {"bid_depth": None, "ask_depth": None}
    bids = book.get("bids") or book.get("buy") or []
    asks = book.get("asks") or book.get("sell") or []

    def top_depth(levels: Any, *, best_high: bool) -> float | None:
        parsed: list[tuple[float, float]] = []
        for level in levels if isinstance(levels, list) else []:
            if isinstance(level, dict):
                price = _to_float(level.get("price"))
                size = _to_float(level.get("size"))
            elif isinstance(level, (list, tuple)) and len(level) >= 2:
                price = _to_float(level[0])
                size = _to_float(level[1])
            else:
                continue
            if price is not None and size is not None:
                parsed.append((price, size))
        if not parsed:
            return None
        parsed.sort(key=lambda x: x[0], reverse=best_high)
        return parsed[0][1]

    return {"bid_depth": top_depth(bids, best_high=True), "ask_depth": top_depth(asks, best_high=False)}


def _side_book(row: sqlite3.Row, side: str) -> dict[str, Any] | None:
    key = f"{side.lower()}_orderbook_json"
    if key in row.keys() and row[key]:
        parsed = _json_loads(row[key])
        if isinstance(parsed, dict):
            return parsed
    for raw_key in ("raw_book_json", "raw_json", "raw_state_json"):
        if raw_key in row.keys() and row[raw_key]:
            parsed = _json_loads(row[raw_key])
            if isinstance(parsed, dict):
                candidate = parsed.get(side.lower()) or parsed.get(f"{side.lower()}_orderbook")
                if isinstance(candidate, dict):
                    return candidate
    return None


def normalize_snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
    yes_book = _side_book(row, "yes")
    no_book = _side_book(row, "no")
    yes_depth = parse_orderbook_depth(yes_book)
    no_depth = parse_orderbook_depth(no_book)
    yes_bid = _to_float(row["yes_bid"]) if "yes_bid" in row.keys() else None
    yes_ask = _to_float(row["yes_ask"]) if "yes_ask" in row.keys() else None
    no_bid = _to_float(row["no_bid"]) if "no_bid" in row.keys() else None
    no_ask = _to_float(row["no_ask"]) if "no_ask" in row.keys() else None
    market_key = None
    for key in ("market_slug", "market_ticker", "condition_id"):
        if key in row.keys() and row[key]:
            market_key = row[key]
            break
    spread = None
    if yes_bid is not None and yes_ask is not None and no_bid is not None and no_ask is not None:
        spread = (yes_ask - yes_bid) + (no_ask - no_bid)
    pair_ask_cost = yes_ask + no_ask if yes_ask is not None and no_ask is not None else None
    pair_bid_credit = yes_bid + no_bid if yes_bid is not None and no_bid is not None else None
    return {
        "venue": "polymarket",
        "ts": row["ts"],
        "market_key": market_key,
        "market_slug": row["market_slug"] if "market_slug" in row.keys() else market_key,
        "condition_id": row["condition_id"] if "condition_id" in row.keys() else None,
        "yes_token_id": row["yes_token_id"] if "yes_token_id" in row.keys() else None,
        "no_token_id": row["no_token_id"] if "no_token_id" in row.keys() else None,
        "market_open_time": row["market_open_time"] if "market_open_time" in row.keys() else None,
        "market_close_time": row["market_close_time"] if "market_close_time" in row.keys() else None,
        "seconds_to_close": _to_float(row["seconds_to_close"]) if "seconds_to_close" in row.keys() else None,
        "btc_price": _to_float(row["btc_price"]) if "btc_price" in row.keys() else None,
        "strike": _to_float(row["strike"]) if "strike" in row.keys() else None,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "yes_bid_depth": yes_depth["bid_depth"],
        "yes_ask_depth": yes_depth["ask_depth"],
        "no_bid_depth": no_depth["bid_depth"],
        "no_ask_depth": no_depth["ask_depth"],
        "spread": round(spread, 6) if spread is not None else None,
        "pair_ask_cost": round(pair_ask_cost, 6) if pair_ask_cost is not None else None,
        "pair_bid_credit": round(pair_bid_credit, 6) if pair_bid_credit is not None else None,
    }


def load_nearest_snapshot(conn: sqlite3.Connection, ts: str, *, market_key: str | None = None, tolerance_seconds: float | None = None) -> dict[str, Any] | None:
    params: list[Any] = [ts]
    where = []
    if market_key:
        where.append("(market_slug=? OR market_ticker=? OR condition_id=?)")
        params.extend([market_key, market_key, market_key])
    sql_where = "WHERE " + " AND ".join(where) if where else ""
    row = conn.execute(
        f"""
        SELECT *, ABS((julianday(ts) - julianday(?)) * 86400.0) AS distance_seconds
        FROM realtime_snapshots_1s
        {sql_where}
        ORDER BY distance_seconds ASC, ts ASC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    if row is None:
        return None
    if tolerance_seconds is not None and row["distance_seconds"] is not None and float(row["distance_seconds"]) > tolerance_seconds:
        return None
    snap = normalize_snapshot_row(row)
    snap["distance_seconds"] = round(float(row["distance_seconds"]), 6) if row["distance_seconds"] is not None else None
    return snap


def load_snapshots(conn: sqlite3.Connection, *, market_key: str | None = None, since: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = []
    if market_key:
        where.append("(market_slug=? OR market_ticker=? OR condition_id=?)")
        params.extend([market_key, market_key, market_key])
    if since:
        where.append("ts >= ?")
        params.append(since)
    sql_where = "WHERE " + " AND ".join(where) if where else ""
    sql_limit = "LIMIT ?" if limit else ""
    if limit:
        params.append(limit)
    rows = conn.execute(f"SELECT * FROM realtime_snapshots_1s {sql_where} ORDER BY ts ASC {sql_limit}", tuple(params)).fetchall()
    return [normalize_snapshot_row(row) for row in rows]
