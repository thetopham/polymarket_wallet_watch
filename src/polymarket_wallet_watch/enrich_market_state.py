from __future__ import annotations

import argparse
import math
import sqlite3
from statistics import pstdev
from typing import Any

from .config import load_config
from .db import connect, initialize_schema
from .util import parse_ts


def _nearest_btc_row(feed_conn: sqlite3.Connection, table: str, ts_col: str, price_cols: list[str], event_ts: str) -> tuple[float | None, list[float]]:
    # Intentionally simple SQLite query; callers choose trusted local BTC/Kalshi feed path.
    cols = [ts_col] + price_cols
    col_sql = ",".join(cols)
    rows = feed_conn.execute(
        f"SELECT {col_sql} FROM {table} WHERE {ts_col} <= ? ORDER BY {ts_col} DESC LIMIT 180",
        (event_ts,),
    ).fetchall()
    prices: list[float] = []
    for row in rows:
        for col in price_cols:
            try:
                val = row[col]
            except Exception:
                val = None
            if val is not None:
                prices.append(float(val))
                break
    return (prices[0] if prices else None), prices


def compute_price_features(prices_desc: list[float]) -> dict[str, float | None]:
    def slope(n: int) -> float | None:
        if len(prices_desc) <= n:
            return None
        return prices_desc[0] - prices_desc[n]

    def realized_vol(n: int) -> float | None:
        vals = list(reversed(prices_desc[:n]))
        if len(vals) < 3:
            return None
        rets = [(vals[i] - vals[i - 1]) / vals[i - 1] for i in range(1, len(vals)) if vals[i - 1]]
        return pstdev(rets) * math.sqrt(len(rets)) if len(rets) > 1 else None

    atr_proxy = None
    if len(prices_desc) >= 60:
        atr_proxy = sum(abs(prices_desc[i] - prices_desc[i + 1]) for i in range(59)) / 59
    return {
        "slope_15s": slope(15),
        "slope_60s": slope(60),
        "slope_180s": slope(179),
        "realized_vol_60s": realized_vol(60),
        "realized_vol_180s": realized_vol(180),
        "atr_proxy": atr_proxy,
    }


def time_bucket(event_ts: str) -> str:
    dt = parse_ts(event_ts)
    if not dt:
        return "unknown"
    return f"{dt.hour:02d}:{(dt.minute // 15) * 15:02d}"


def enrich_event(conn: sqlite3.Connection, event: sqlite3.Row, btc_price: float | None, prices_desc: list[float]) -> dict[str, Any]:
    snap = conn.execute(
        "SELECT * FROM market_snapshots WHERE token_id=? AND snapshot_ts <= ? ORDER BY snapshot_ts DESC LIMIT 1",
        (event["token_id"], event["event_ts"]),
    ).fetchone()
    market = conn.execute("SELECT * FROM markets WHERE market_id=?", (event["market_id"],)).fetchone() if event["market_id"] else None
    strike = market["strike"] if market and market["strike"] is not None else None
    distance = (btc_price - strike) if btc_price is not None and strike is not None else None
    feats = compute_price_features(prices_desc)
    row = {
        "event_id": event["id"],
        "wallet_address": event["wallet_address"],
        "market_id": event["market_id"],
        "condition_id": event["condition_id"],
        "token_id": event["token_id"],
        "event_ts": event["event_ts"],
        "btc_price": btc_price,
        "strike": strike,
        "distance_from_strike": distance,
        "distance_bps": (distance / strike * 10000) if distance is not None and strike else None,
        "time_bucket": time_bucket(event["event_ts"]),
        "enrichment_version": "v0.1",
        **feats,
    }
    if snap:
        row.update({
            "orderbook_spread": snap["spread"],
            "top_of_book_liquidity": snap["top_of_book_liquidity"],
            "imbalance": snap["imbalance"],
            "yes_mid": snap["yes_mid"],
            "no_mid": snap["no_mid"],
        })
    return row


def upsert_enriched(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    cols = list(row.keys())
    placeholders = ",".join(["?"] * len(cols))
    update = ",".join([f"{c}=excluded.{c}" for c in cols if c != "event_id"])
    conn.execute(
        f"INSERT INTO enriched_wallet_events({','.join(cols)}) VALUES({placeholders}) ON CONFLICT(event_id) DO UPDATE SET {update}",
        tuple(row[c] for c in cols),
    )
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich wallet events with local BTC/Kalshi feed and CLOB state.")
    parser.add_argument("--config", default="config.example.yaml")
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    cfg = load_config(args.config)
    conn = connect(cfg.db_path)
    initialize_schema(conn)
    feed_cfg = cfg.data.get("btc_feed", {})
    feed = sqlite3.connect(feed_cfg.get("sqlite_path"))
    feed.row_factory = sqlite3.Row
    table = feed_cfg.get("table", "snapshots")
    ts_col = feed_cfg.get("timestamp_column", "ts")
    price_cols = feed_cfg.get("price_column_candidates", ["btc_price"])
    count = 0
    for event in conn.execute("SELECT * FROM wallet_events ORDER BY event_ts DESC LIMIT ?", (args.limit,)):
        btc_price, prices = _nearest_btc_row(feed, table, ts_col, price_cols, event["event_ts"])
        upsert_enriched(conn, enrich_event(conn, event, btc_price, prices))
        count += 1
    print(f"enriched_events={count}")


if __name__ == "__main__":
    main()
