from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx

from .config import load_config
from .db import connect, initialize_schema
from .util import dump_json, iso_ts, to_float, write_raw_json


def normalize_market(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_id": str(raw.get("id") or raw.get("marketId") or raw.get("conditionId") or ""),
        "condition_id": raw.get("conditionId") or raw.get("condition_id"),
        "question_id": raw.get("questionID") or raw.get("questionId") or raw.get("question_id"),
        "slug": raw.get("slug"),
        "title": raw.get("question") or raw.get("title") or raw.get("name"),
        "event_slug": (raw.get("events") or [{}])[0].get("slug") if isinstance(raw.get("events"), list) and raw.get("events") else raw.get("eventSlug"),
        "category": raw.get("category"),
        "active": int(bool(raw.get("active"))) if raw.get("active") is not None else None,
        "closed": int(bool(raw.get("closed"))) if raw.get("closed") is not None else None,
        "start_ts": iso_ts(raw.get("startDate") or raw.get("start_date")),
        "close_ts": iso_ts(raw.get("endDate") or raw.get("closeTime") or raw.get("end_date")),
        "end_ts": iso_ts(raw.get("endDate") or raw.get("end_date")),
        "strike": to_float(raw.get("strike") or raw.get("priceToBeat")),
        "raw_json": dump_json(raw),
        "source": "gamma",
    }


def upsert_market(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    if not row.get("market_id"):
        return
    conn.execute(
        """
        INSERT INTO markets(market_id, condition_id, question_id, slug, title, event_slug, category, active, closed,
                            start_ts, close_ts, end_ts, strike, raw_json, source)
        VALUES(:market_id, :condition_id, :question_id, :slug, :title, :event_slug, :category, :active, :closed,
               :start_ts, :close_ts, :end_ts, :strike, :raw_json, :source)
        ON CONFLICT(market_id) DO UPDATE SET
            condition_id=excluded.condition_id,
            question_id=excluded.question_id,
            slug=excluded.slug,
            title=excluded.title,
            event_slug=excluded.event_slug,
            category=excluded.category,
            active=excluded.active,
            closed=excluded.closed,
            start_ts=excluded.start_ts,
            close_ts=excluded.close_ts,
            end_ts=excluded.end_ts,
            strike=excluded.strike,
            raw_json=excluded.raw_json,
            source=excluded.source,
            updated_at=datetime('now')
        """,
        row,
    )
    conn.commit()


def fetch_markets(config: dict[str, Any]) -> list[dict[str, Any]]:
    pm = config.get("polymarket", {})
    base = pm.get("gamma_base_url", "https://gamma-api.polymarket.com").rstrip("/")
    params = pm.get("markets_query", {})
    headers = {"User-Agent": pm.get("user_agent", "polymarket-wallet-watch/0.1")}
    with httpx.Client(timeout=20, headers=headers) as client:
        resp = client.get(f"{base}/markets", params=params)
        resp.raise_for_status()
        payload = resp.json()
    if isinstance(payload, dict):
        return payload.get("markets") or payload.get("data") or []
    return payload


def ingest_markets(config_path: str = "config.example.yaml") -> int:
    cfg = load_config(config_path)
    conn = connect(cfg.db_path)
    initialize_schema(conn)
    raw = fetch_markets(cfg.data)
    write_raw_json(cfg.data.get("database", {}).get("raw_response_dir", "raw"), "gamma-markets", raw)
    count = 0
    for item in raw:
        row = normalize_market(item)
        if row.get("market_id"):
            upsert_market(conn, row)
            count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest Polymarket Gamma market metadata (read-only).")
    parser.add_argument("--config", default="config.example.yaml")
    args = parser.parse_args()
    print(f"ingested_markets={ingest_markets(args.config)}")


if __name__ == "__main__":
    main()
