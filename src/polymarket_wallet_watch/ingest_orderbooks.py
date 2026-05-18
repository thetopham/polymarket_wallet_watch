from __future__ import annotations

import argparse
import sqlite3
from typing import Any

import httpx

from .config import load_config
from .db import connect, initialize_schema
from .util import dump_json, to_float, utc_now_iso, write_raw_json


def normalize_orderbook_snapshot(token_id: str, raw: dict[str, Any], snapshot_ts: str | None = None) -> dict[str, Any]:
    bids = raw.get("bids") or raw.get("buy") or []
    asks = raw.get("asks") or raw.get("sell") or []

    def best(levels: list[dict[str, Any]], reverse: bool) -> tuple[float | None, float | None]:
        parsed = [(to_float(x.get("price")), to_float(x.get("size"))) for x in levels if isinstance(x, dict)]
        parsed = [(p, s) for p, s in parsed if p is not None]
        if not parsed:
            return None, None
        parsed.sort(key=lambda x: x[0], reverse=reverse)
        return parsed[0]

    bid, bid_size = best(bids, True)
    ask, ask_size = best(asks, False)
    spread = ask - bid if bid is not None and ask is not None else None
    mid = (ask + bid) / 2 if bid is not None and ask is not None else None
    liq = (bid_size or 0) + (ask_size or 0) if bid_size is not None or ask_size is not None else None
    imbalance = ((bid_size or 0) - (ask_size or 0)) / liq if liq else None
    return {
        "token_id": token_id,
        "snapshot_ts": snapshot_ts or utc_now_iso(),
        "yes_bid": bid,
        "yes_ask": ask,
        "yes_mid": mid,
        "spread": spread,
        "top_bid_size": bid_size,
        "top_ask_size": ask_size,
        "top_of_book_liquidity": liq,
        "imbalance": imbalance,
        "raw_json": dump_json(raw),
        "source": "clob",
    }


def insert_snapshot(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    cols = ",".join(row.keys())
    placeholders = ",".join(["?"] * len(row))
    conn.execute(f"INSERT OR REPLACE INTO market_snapshots({cols}) VALUES({placeholders})", tuple(row.values()))
    conn.commit()


def fetch_orderbook(config: dict[str, Any], token_id: str) -> dict[str, Any]:
    pm = config.get("polymarket", {})
    base = pm.get("clob_base_url", "https://clob.polymarket.com").rstrip("/")
    headers = {"User-Agent": pm.get("user_agent", "polymarket-wallet-watch/0.1")}
    with httpx.Client(timeout=20, headers=headers) as client:
        resp = client.get(f"{base}/book", params={"token_id": token_id})
        resp.raise_for_status()
        return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch public CLOB books for token IDs (read-only).")
    parser.add_argument("--config", default="config.example.yaml")
    parser.add_argument("--token-id", action="append", required=True)
    args = parser.parse_args()
    cfg = load_config(args.config)
    conn = connect(cfg.db_path)
    initialize_schema(conn)
    for token_id in args.token_id:
        raw = fetch_orderbook(cfg.data, token_id)
        write_raw_json(cfg.data.get("database", {}).get("raw_response_dir", "raw"), f"book-{token_id}", raw)
        insert_snapshot(conn, normalize_orderbook_snapshot(token_id, raw))
        print(f"inserted_book token_id={token_id}")


if __name__ == "__main__":
    main()
