from __future__ import annotations

import argparse
import sqlite3
from typing import Any

import httpx

from .config import load_config
from .db import connect, initialize_schema, upsert_wallet
from .report import insert_wallet_event
from .util import dump_json, iso_ts, seconds_between, to_float, write_raw_json


def _nested_market(raw: dict[str, Any]) -> dict[str, Any]:
    market = raw.get("market")
    return market if isinstance(market, dict) else {}


def normalize_side(outcome: Any) -> str:
    text = str(outcome or "").strip().upper()
    if text in {"YES", "Y", "UP", "ABOVE"}:
        return "YES"
    if text in {"NO", "N", "DOWN", "BELOW"}:
        return "NO"
    return "UNKNOWN"


def normalize_action(side_or_action: Any) -> str:
    text = str(side_or_action or "").strip().lower()
    if text in {"buy", "bought", "mint"}:
        return "buy"
    if text in {"sell", "sold", "redeem"}:
        return "sell"
    if text in {"add", "reduce", "exit"}:
        return text
    return "unknown"


def normalize_wallet_trade(wallet_address: str, raw: dict[str, Any]) -> dict[str, Any]:
    market = _nested_market(raw)
    event_ts = iso_ts(raw.get("timestamp") or raw.get("createdAt") or raw.get("created_at") or raw.get("time"))
    close_ts = market.get("endDate") or market.get("closeTime") or raw.get("market_end") or raw.get("endDate")
    price = to_float(raw.get("price") or raw.get("avgPrice") or raw.get("lastPrice"))
    size = to_float(raw.get("size") or raw.get("amount") or raw.get("shares"))
    outcome = raw.get("outcome") or raw.get("outcomeName") or raw.get("sideOutcome")
    side = normalize_side(outcome)
    action = normalize_action(raw.get("side") or raw.get("action") or raw.get("type"))
    return {
        "wallet_address": wallet_address.lower(),
        "market_id": str(market.get("id") or raw.get("market_id") or raw.get("marketId") or "") or None,
        "condition_id": raw.get("conditionId") or raw.get("condition_id") or market.get("conditionId"),
        "token_id": raw.get("asset") or raw.get("token_id") or raw.get("tokenId") or raw.get("asset_id"),
        "event_ts": event_ts,
        "side": side,
        "action": action,
        "price": price,
        "size": size,
        "notional": (price * size) if price is not None and size is not None else None,
        "aggressor_side": raw.get("aggressorSide") or raw.get("aggressor_side"),
        "tx_hash": raw.get("transactionHash") or raw.get("tx_hash") or raw.get("transaction_hash"),
        "trade_id": str(raw.get("id") or raw.get("tradeId") or raw.get("trade_id") or "") or None,
        "source": "polymarket_clob",
        "seconds_to_close": seconds_between(event_ts, close_ts) if event_ts else None,
        "market_slug": market.get("slug") or raw.get("market_slug") or raw.get("slug"),
        "market_title": market.get("question") or market.get("title") or raw.get("title") or raw.get("market_title"),
        "outcome": str(outcome) if outcome is not None else None,
        "raw_json": dump_json(raw),
    }


def fetch_wallet_trades(config: dict[str, Any], wallet_address: str) -> list[dict[str, Any]]:
    pm = config.get("polymarket", {})
    base = pm.get("data_api_base_url", "https://data-api.polymarket.com").rstrip("/")
    limit = pm.get("wallet_trade_limit", 500)
    headers = {"User-Agent": pm.get("user_agent", "polymarket-wallet-watch/0.1")}
    endpoints = [
        (f"{base}/trades", {"user": wallet_address, "limit": limit}),
        (f"{base}/activity", {"user": wallet_address, "limit": limit}),
    ]
    last_error: Exception | None = None
    with httpx.Client(timeout=30, headers=headers) as client:
        for url, params in endpoints:
            try:
                resp = client.get(url, params=params)
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                payload = resp.json()
                if isinstance(payload, dict):
                    return payload.get("trades") or payload.get("data") or payload.get("activity") or []
                return payload
            except Exception as exc:  # pragma: no cover - live endpoint fallback
                last_error = exc
                continue
    if last_error:
        raise last_error
    return []


def ingest_wallets(config_path: str = "config.example.yaml") -> int:
    cfg = load_config(config_path)
    conn = connect(cfg.db_path)
    initialize_schema(conn)
    total = 0
    for item in cfg.data.get("wallets", []):
        if not item.get("enabled", True):
            continue
        wallet = item["address"].lower()
        upsert_wallet(conn, wallet, item.get("label"), enabled=True)
        trades = fetch_wallet_trades(cfg.data, wallet)
        write_raw_json(cfg.data.get("database", {}).get("raw_response_dir", "raw"), f"wallet-{wallet}", trades)
        for raw in trades:
            event = normalize_wallet_trade(wallet, raw)
            if event.get("event_ts"):
                insert_wallet_event(conn, event, or_ignore=True)
                total += 1
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest public wallet activity/trades (read-only).")
    parser.add_argument("--config", default="config.example.yaml")
    args = parser.parse_args()
    print(f"ingested_wallet_events={ingest_wallets(args.config)}")


if __name__ == "__main__":
    main()
