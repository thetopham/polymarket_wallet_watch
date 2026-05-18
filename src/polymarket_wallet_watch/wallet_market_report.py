from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import load_config
from .db import connect, initialize_schema

BUY_ACTIONS = {"buy", "add"}
SELL_ACTIONS = {"sell", "reduce", "exit"}
SIDES = ("YES", "NO")


def _parse_since(since: str | None) -> str | None:
    if not since:
        return None
    text = since.strip().lower()
    if text.endswith("h") and text[:-1].isdigit():
        return (datetime.now(timezone.utc) - timedelta(hours=int(text[:-1]))).isoformat()
    if text.endswith("d") and text[:-1].isdigit():
        return (datetime.now(timezone.utc) - timedelta(days=int(text[:-1]))).isoformat()
    return since


def _round(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _weighted_avg(notional: float, qty: float) -> float | None:
    if abs(qty) <= 1e-12:
        return None
    return notional / qty


def _market_matches_filters(slug: str | None, *, asset: str | None, interval: str | None) -> bool:
    slug_l = (slug or "").lower()
    if asset and not slug_l.startswith(asset.lower()):
        return False
    if interval and f"-{interval.lower()}-" not in slug_l:
        return False
    return True


def _fetch_events(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    market_slug: str,
    since: str | None = None,
    asset: str | None = None,
    interval: str | None = None,
) -> list[sqlite3.Row]:
    cutoff = _parse_since(since)
    clauses = ["LOWER(wallet_address) = LOWER(?)", "market_slug = ?"]
    params: list[Any] = [wallet, market_slug]
    if cutoff:
        clauses.append("event_ts >= ?")
        params.append(cutoff)
    if asset:
        clauses.append("LOWER(COALESCE(market_slug, '')) LIKE ?")
        params.append(f"{asset.lower()}%")
    if interval:
        clauses.append("LOWER(COALESCE(market_slug, '')) LIKE ?")
        params.append(f"%-{interval.lower()}-%")
    where = " AND ".join(clauses)
    return conn.execute(
        f"""
        SELECT id, wallet_address, market_id, condition_id, token_id, event_ts, side, action,
               price, size, notional, aggressor_side, tx_hash, trade_id, source,
               seconds_to_close, market_slug, market_title, outcome
        FROM wallet_events
        WHERE {where}
        ORDER BY event_ts ASC, id ASC
        """,
        tuple(params),
    ).fetchall()


def build_wallet_market_inventory(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    market_slug: str,
    since: str | None = None,
    asset: str | None = None,
    interval: str | None = None,
) -> dict[str, Any]:
    if not _market_matches_filters(market_slug, asset=asset, interval=interval):
        rows: list[sqlite3.Row] = []
    else:
        rows = _fetch_events(conn, wallet=wallet, market_slug=market_slug, since=since, asset=asset, interval=interval)

    buy_qty = {side: 0.0 for side in SIDES}
    buy_notional = {side: 0.0 for side in SIDES}
    open_qty = {side: 0.0 for side in SIDES}
    open_cost = {side: 0.0 for side in SIDES}
    max_swing = {side: 0.0 for side in SIDES}
    first_entry_ts: str | None = None
    last_entry_ts: str | None = None
    timeline: list[dict[str, Any]] = []

    for row in rows:
        side = (row["side"] or "UNKNOWN").upper()
        action = (row["action"] or "unknown").lower()
        size = float(row["size"] or 0.0)
        price = row["price"]
        price_f = float(price) if price is not None else None
        notional = row["notional"]
        notional_f = float(notional) if notional is not None else (size * price_f if price_f is not None else 0.0)

        if side in SIDES and action in BUY_ACTIONS:
            buy_qty[side] += size
            buy_notional[side] += notional_f
            open_qty[side] += size
            open_cost[side] += notional_f
            if first_entry_ts is None:
                first_entry_ts = row["event_ts"]
            last_entry_ts = row["event_ts"]
        elif side in SIDES and action in SELL_ACTIONS:
            sell_qty = min(size, open_qty[side]) if open_qty[side] > 0 else size
            avg_open = _weighted_avg(open_cost[side], open_qty[side]) or 0.0
            open_qty[side] -= size
            open_cost[side] -= avg_open * sell_qty
            if open_qty[side] <= 1e-12:
                open_qty[side] = 0.0
                open_cost[side] = 0.0

        if side in SIDES:
            max_swing[side] = max(max_swing[side], abs(open_qty[side]))

        timeline.append(
            {
                "id": row["id"],
                "event_ts": row["event_ts"],
                "side": side,
                "action": action,
                "price": price_f,
                "size": size,
                "notional": notional_f,
                "open_yes_after": _round(open_qty["YES"]),
                "open_no_after": _round(open_qty["NO"]),
                "seconds_to_close": row["seconds_to_close"],
                "trade_id": row["trade_id"],
                "tx_hash": row["tx_hash"],
                "source": row["source"],
            }
        )

    yes_avg = _weighted_avg(buy_notional["YES"], buy_qty["YES"])
    no_avg = _weighted_avg(buy_notional["NO"], buy_qty["NO"])
    matched_qty = min(buy_qty["YES"], buy_qty["NO"])
    matched_cost = (yes_avg + no_avg) if yes_avg is not None and no_avg is not None else None
    locked_profit = matched_qty * (1 - matched_cost) if matched_cost is not None else None

    unpaired_yes = max(buy_qty["YES"] - buy_qty["NO"], 0.0)
    unpaired_no = max(buy_qty["NO"] - buy_qty["YES"], 0.0)
    if unpaired_yes > 1e-12:
        remaining_exposure = "directional_yes"
        unpaired_avg = yes_avg
    elif unpaired_no > 1e-12:
        remaining_exposure = "directional_no"
        unpaired_avg = no_avg
    elif matched_qty > 0:
        remaining_exposure = "balanced_pair"
        unpaired_avg = None
    else:
        remaining_exposure = "none"
        unpaired_avg = None

    market_title = rows[0]["market_title"] if rows else None
    market_id = rows[0]["market_id"] if rows else None
    condition_id = rows[0]["condition_id"] if rows else None

    return {
        "wallet_address": wallet.lower(),
        "market_slug": market_slug,
        "market_title": market_title,
        "market_id": market_id,
        "condition_id": condition_id,
        "since": since,
        "asset": asset,
        "interval": interval,
        "event_count": len(rows),
        "yes_buys": {"qty": _round(buy_qty["YES"]), "avg_price": _round(yes_avg), "notional": _round(buy_notional["YES"])},
        "no_buys": {"qty": _round(buy_qty["NO"]), "avg_price": _round(no_avg), "notional": _round(buy_notional["NO"])},
        "matched_pair_qty": _round(matched_qty),
        "matched_pair_cost": _round(matched_cost),
        "locked_profit_if_held": _round(locked_profit),
        "created_pair_below_one": bool(matched_cost is not None and matched_qty > 0 and matched_cost < 1.0),
        "unpaired_yes_qty": _round(unpaired_yes),
        "unpaired_no_qty": _round(unpaired_no),
        "unpaired_avg_price": _round(unpaired_avg),
        "remaining_exposure": remaining_exposure,
        "event_timeline": timeline,
        "max_inventory_swing": {"YES": _round(max_swing["YES"]), "NO": _round(max_swing["NO"])},
        "first_entry_ts": first_entry_ts,
        "last_entry_ts": last_entry_ts,
        "safety": "READ_ONLY_RESEARCH: no private keys, no orders, no live trading.",
    }


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def format_wallet_market_report(report: dict[str, Any]) -> str:
    yes = report["yes_buys"]
    no = report["no_buys"]
    created_pair = "YES" if report["created_pair_below_one"] else "NO"
    if report["remaining_exposure"] == "directional_yes":
        directional_side = "YES"
    elif report["remaining_exposure"] == "directional_no":
        directional_side = "NO"
    else:
        directional_side = "none"
    lines = [
        "Polymarket Wallet Market-Window Inventory — READ-ONLY RESEARCH",
        "Safety: no private keys, no live orders, no automatic trading.",
        f"Wallet: {report['wallet_address']}",
        f"Market: {report['market_slug']}" + (f" | {report['market_title']}" if report.get("market_title") else ""),
        f"Filters: since={report.get('since') or '-'} asset={report.get('asset') or '-'} interval={report.get('interval') or '-'}",
        f"Events: {report['event_count']}",
        "",
        "Buy inventory:",
        f"  YES buys: qty={_fmt(yes['qty'])} avg_price={_fmt(yes['avg_price'])} notional={_fmt(yes['notional'])}",
        f"  NO  buys: qty={_fmt(no['qty'])} avg_price={_fmt(no['avg_price'])} notional={_fmt(no['notional'])}",
        "",
        "Matched pair:",
        f"  matched_pair_qty={_fmt(report['matched_pair_qty'])}",
        f"  matched_pair_cost={_fmt(report['matched_pair_cost'])}",
        f"  locked_profit_if_held={_fmt(report['locked_profit_if_held'])}",
        f"  YES+NO pair below 1.00: {created_pair}",
        "",
        "Remaining exposure:",
        f"  Remaining exposure: {report['remaining_exposure']}",
        f"  unpaired_yes_qty={_fmt(report['unpaired_yes_qty'])}",
        f"  unpaired_no_qty={_fmt(report['unpaired_no_qty'])}",
        f"  unpaired_avg_price={_fmt(report['unpaired_avg_price'])}",
        f"  Directional remainder: {directional_side}",
        "",
        "Timing and inventory swing:",
        f"  first_entry_ts={report.get('first_entry_ts') or '-'}",
        f"  last_entry_ts={report.get('last_entry_ts') or '-'}",
        f"  max_inventory_swing YES={_fmt(report['max_inventory_swing']['YES'])} NO={_fmt(report['max_inventory_swing']['NO'])}",
        "",
        "Interpretation:",
    ]
    if report["created_pair_below_one"]:
        lines.append("  The wallet created a YES+NO matched pair below 1.00 in the observed event stream.")
    else:
        lines.append("  No below-1.00 matched YES+NO pair was detected in the observed event stream.")
    if report["remaining_exposure"] == "directional_yes":
        lines.append("  Remaining buy inventory is directionally YES after matched-pair allocation.")
    elif report["remaining_exposure"] == "directional_no":
        lines.append("  Remaining buy inventory is directionally NO after matched-pair allocation.")
    elif report["remaining_exposure"] == "balanced_pair":
        lines.append("  Buy inventory is balanced by side after matched-pair allocation.")
    else:
        lines.append("  No buy inventory was detected for this wallet/market/filter set.")
    lines += [
        "  Warning: this is event-reconstructed inventory, not verified position state unless reconciled elsewhere.",
        "",
        "Event timeline ascending:",
    ]
    if not report["event_timeline"]:
        lines.append("  No wallet events matched the requested wallet/market/filter set.")
    else:
        for ev in report["event_timeline"]:
            lines.append(
                f"  {ev['event_ts']} | {ev['side']} {ev['action']} | px={_fmt(ev['price'])} "
                f"size={_fmt(ev['size'])} notional={_fmt(ev['notional'])} | "
                f"open_after YES={_fmt(ev['open_yes_after'])} NO={_fmt(ev['open_no_after'])} | "
                f"trade_id={ev.get('trade_id') or '-'} src={ev.get('source') or '-'}"
            )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Report one wallet's inventory in one Polymarket contract window.")
    parser.add_argument("--config", default="config.example.yaml")
    parser.add_argument("--wallet", required=True)
    parser.add_argument("--market", required=True, help="market_slug, e.g. btc-updown-5m-1779129000")
    parser.add_argument("--since", default=None, help="e.g. 24h, 7d, or ISO timestamp")
    parser.add_argument("--asset", default=None, help="optional asset slug prefix, e.g. btc")
    parser.add_argument("--interval", default=None, help="optional interval embedded in slug, e.g. 5m")
    args = parser.parse_args()

    cfg = load_config(args.config)
    conn = connect(cfg.db_path)
    initialize_schema(conn)
    report = build_wallet_market_inventory(
        conn,
        wallet=args.wallet,
        market_slug=args.market,
        since=args.since,
        asset=args.asset,
        interval=args.interval,
    )
    print(format_wallet_market_report(report))


if __name__ == "__main__":
    main()
