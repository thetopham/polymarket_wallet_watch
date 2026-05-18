from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .adapter_polymarket_1s import DEFAULT_POLYMARKET_1S_DB, connect_feed, load_nearest_snapshot
from .config import load_config
from .db import connect, initialize_schema

BTC_15M_RE = re.compile(r"^btc-updown-15m-(\d{10})$")
BUY_ACTIONS = {"buy", "add"}
SELL_ACTIONS = {"sell", "reduce", "exit"}
SIDES = {"YES", "NO"}


@dataclass
class FillContext:
    event_id: int
    event_ts: str
    market_slug: str
    seconds_after_open: float | None
    seconds_before_close: float | None
    phase: str
    side: str
    action: str
    price: float | None
    size: float
    notional: float
    yes_avg_after: float | None
    no_avg_after: float | None
    matched_qty_after: float
    matched_pair_cost_after: float | None
    matched_edge_after: float | None
    unpaired_side_after: str | None
    unpaired_qty_after: float
    fill_role: str
    projected_pair_cost_before_fill: float | None
    projected_pair_cost_after_fill: float | None
    orderbook_ts: str | None
    orderbook_distance_seconds: float | None
    orderbook_class: str
    side_bid: float | None
    side_ask: float | None
    side_bid_depth: float | None
    side_ask_depth: float | None
    pair_ask_cost: float | None
    pair_bid_credit: float | None
    btc_price: float | None
    strike: float | None


@dataclass
class MarketSummary:
    market_slug: str
    first_fill_ts: str | None
    last_fill_ts: str | None
    first_seconds_after_open: float | None
    last_seconds_after_open: float | None
    last_seconds_before_close: float | None
    fill_count: int
    total_notional: float
    yes_qty: float
    yes_notional: float
    yes_avg: float | None
    no_qty: float
    no_notional: float
    no_avg: float | None
    matched_qty: float
    matched_pair_cost: float | None
    matched_edge_if_held: float | None
    unpaired_side: str | None
    unpaired_qty: float
    open_notional: float
    mid_notional: float
    late_notional: float
    open_share: float | None
    mid_share: float | None
    late_share: float | None
    near_bid_fills: int
    near_ask_fills: int
    outside_book_fills: int
    no_book_fills: int
    passive_fill_share: float | None
    avg_pair_ask_cost_at_fills: float | None
    min_pair_ask_cost_at_fills: float | None
    max_abs_btc_slope_60s: float | None
    strategy_tags: list[str]


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _round(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _market_window_from_slug(slug: str) -> tuple[datetime, datetime] | None:
    match = BTC_15M_RE.match(slug or "")
    if not match:
        return None
    start = datetime.fromtimestamp(int(match.group(1)), timezone.utc)
    return start, start + timedelta(seconds=900)


def _phase(seconds_after_open: float | None) -> str:
    if seconds_after_open is None:
        return "unknown"
    if seconds_after_open < 180:
        return "open"
    if seconds_after_open < 720:
        return "mid"
    return "late"


def _weighted_avg(notional: float, qty: float) -> float | None:
    if abs(qty) <= 1e-12:
        return None
    return notional / qty


def _unpaired(yes_qty: float, no_qty: float) -> tuple[str | None, float]:
    if yes_qty > no_qty + 1e-12:
        return "YES", yes_qty - no_qty
    if no_qty > yes_qty + 1e-12:
        return "NO", no_qty - yes_qty
    return None, 0.0


def _pair_cost(yes_notional: float, yes_qty: float, no_notional: float, no_qty: float) -> float | None:
    yes_avg = _weighted_avg(yes_notional, yes_qty)
    no_avg = _weighted_avg(no_notional, no_qty)
    if yes_avg is None or no_avg is None:
        return None
    return yes_avg + no_avg


def _project_pair_cost_after_fill(
    *,
    side: str,
    action: str,
    size: float,
    notional: float,
    yes_qty: float,
    yes_notional: float,
    no_qty: float,
    no_notional: float,
) -> float | None:
    if side not in SIDES or action not in BUY_ACTIONS:
        return _pair_cost(yes_notional, yes_qty, no_notional, no_qty)
    if side == "YES":
        yes_qty += size
        yes_notional += notional
    else:
        no_qty += size
        no_notional += notional
    return _pair_cost(yes_notional, yes_qty, no_notional, no_qty)


def _classify_orderbook(side: str, price: float | None, snap: dict[str, Any] | None, *, tolerance: float = 0.011) -> tuple[str, float | None, float | None, float | None, float | None]:
    if snap is None:
        return "no_book", None, None, None, None
    if side == "YES":
        bid = snap.get("yes_bid")
        ask = snap.get("yes_ask")
        bid_depth = snap.get("yes_bid_depth")
        ask_depth = snap.get("yes_ask_depth")
    elif side == "NO":
        bid = snap.get("no_bid")
        ask = snap.get("no_ask")
        bid_depth = snap.get("no_bid_depth")
        ask_depth = snap.get("no_ask_depth")
    else:
        return "unknown_side", None, None, None, None
    if price is None:
        return "missing_price", bid, ask, bid_depth, ask_depth
    if bid is not None and abs(price - float(bid)) <= tolerance:
        return "near_bid_passive", bid, ask, bid_depth, ask_depth
    if ask is not None and abs(price - float(ask)) <= tolerance:
        return "near_ask_taker_or_crossed", bid, ask, bid_depth, ask_depth
    if bid is not None and ask is not None and float(bid) < price < float(ask):
        return "inside_spread", bid, ask, bid_depth, ask_depth
    return "outside_top_of_book", bid, ask, bid_depth, ask_depth


def _fill_role(
    *,
    phase: str,
    side: str,
    yes_qty_before: float,
    no_qty_before: float,
    pair_before: float | None,
    pair_after: float | None,
) -> str:
    if yes_qty_before <= 1e-12 and no_qty_before <= 1e-12:
        return "open_seed" if phase == "open" else f"{phase}_seed"
    smaller_side = "YES" if yes_qty_before < no_qty_before else "NO" if no_qty_before < yes_qty_before else None
    improves_pair = pair_before is not None and pair_after is not None and pair_after < pair_before - 1e-9
    worsens_pair = pair_before is not None and pair_after is not None and pair_after > pair_before + 1e-9
    if smaller_side == side:
        if pair_after is not None and pair_after <= 0.95:
            return "pair_repair_under_095"
        return "pair_repair"
    if improves_pair:
        return "average_down_pair"
    if pair_after is not None and pair_after <= 0.95:
        return "add_under_095"
    if worsens_pair:
        return "directional_or_expensive_add"
    return "inventory_add"


def _fetch_btc15m_events(conn: sqlite3.Connection, wallet: str, *, limit: int | None = None) -> list[sqlite3.Row]:
    sql_limit = "LIMIT ?" if limit else ""
    params: list[Any] = [wallet.lower()]
    if limit:
        params.append(limit)
    return conn.execute(
        f"""
        SELECT we.*, ewe.slope_60s AS enriched_slope_60s
        FROM wallet_events we
        LEFT JOIN enriched_wallet_events ewe ON ewe.event_id = we.id
        WHERE LOWER(we.wallet_address) = ?
          AND we.market_slug LIKE 'btc-updown-15m-%'
        ORDER BY we.event_ts ASC, we.id ASC
        {sql_limit}
        """,
        tuple(params),
    ).fetchall()


def build_btc15m_deconstruction(
    conn: sqlite3.Connection,
    feed_conn: sqlite3.Connection | None,
    *,
    wallet: str,
    tolerance_seconds: float = 2.0,
    limit: int | None = None,
) -> dict[str, Any]:
    events = _fetch_btc15m_events(conn, wallet, limit=limit)
    state: dict[str, dict[str, float]] = {}
    fill_contexts: list[FillContext] = []
    per_market_fill_contexts: dict[str, list[FillContext]] = {}
    slopes_by_market: dict[str, list[float]] = {}

    for row in events:
        slug = row["market_slug"]
        window = _market_window_from_slug(slug)
        if window is None:
            continue
        start, close = window
        event_dt = _parse_ts(row["event_ts"])
        seconds_after_open = (event_dt - start).total_seconds()
        seconds_before_close = (close - event_dt).total_seconds()
        phase = _phase(seconds_after_open)
        side = (row["side"] or "UNKNOWN").upper()
        action = (row["action"] or "unknown").lower()
        size = float(row["size"] or 0.0)
        price = float(row["price"]) if row["price"] is not None else None
        notional = float(row["notional"] or (size * price if price is not None else 0.0))
        st = state.setdefault(slug, {"yes_qty": 0.0, "yes_notional": 0.0, "no_qty": 0.0, "no_notional": 0.0})
        yes_qty_before = st["yes_qty"]
        no_qty_before = st["no_qty"]
        pair_before = _pair_cost(st["yes_notional"], st["yes_qty"], st["no_notional"], st["no_qty"])
        pair_after_projected = _project_pair_cost_after_fill(
            side=side,
            action=action,
            size=size,
            notional=notional,
            yes_qty=st["yes_qty"],
            yes_notional=st["yes_notional"],
            no_qty=st["no_qty"],
            no_notional=st["no_notional"],
        )

        if side in SIDES and action in BUY_ACTIONS:
            if side == "YES":
                st["yes_qty"] += size
                st["yes_notional"] += notional
            else:
                st["no_qty"] += size
                st["no_notional"] += notional
        elif side in SIDES and action in SELL_ACTIONS:
            # Public sell events reduce reconstructed open inventory at average cost.
            if side == "YES":
                avg = _weighted_avg(st["yes_notional"], st["yes_qty"]) or 0.0
                reduce_qty = min(size, st["yes_qty"])
                st["yes_qty"] = max(0.0, st["yes_qty"] - size)
                st["yes_notional"] = max(0.0, st["yes_notional"] - avg * reduce_qty)
            else:
                avg = _weighted_avg(st["no_notional"], st["no_qty"]) or 0.0
                reduce_qty = min(size, st["no_qty"])
                st["no_qty"] = max(0.0, st["no_qty"] - size)
                st["no_notional"] = max(0.0, st["no_notional"] - avg * reduce_qty)

        yes_avg_after = _weighted_avg(st["yes_notional"], st["yes_qty"])
        no_avg_after = _weighted_avg(st["no_notional"], st["no_qty"])
        pair_after = _pair_cost(st["yes_notional"], st["yes_qty"], st["no_notional"], st["no_qty"])
        matched_qty = min(st["yes_qty"], st["no_qty"])
        matched_edge = matched_qty * (1.0 - pair_after) if pair_after is not None else None
        unpaired_side, unpaired_qty = _unpaired(st["yes_qty"], st["no_qty"])

        snap = load_nearest_snapshot(feed_conn, row["event_ts"], market_key=slug, tolerance_seconds=tolerance_seconds) if feed_conn is not None else None
        ob_class, side_bid, side_ask, side_bid_depth, side_ask_depth = _classify_orderbook(side, price, snap)
        role = _fill_role(
            phase=phase,
            side=side,
            yes_qty_before=yes_qty_before,
            no_qty_before=no_qty_before,
            pair_before=pair_before,
            pair_after=pair_after_projected,
        )
        slope = row["enriched_slope_60s"]
        if slope is not None:
            slopes_by_market.setdefault(slug, []).append(abs(float(slope)))

        ctx = FillContext(
            event_id=int(row["id"]),
            event_ts=row["event_ts"],
            market_slug=slug,
            seconds_after_open=_round(seconds_after_open, 3),
            seconds_before_close=_round(seconds_before_close, 3),
            phase=phase,
            side=side,
            action=action,
            price=price,
            size=_round(size) or 0.0,
            notional=_round(notional) or 0.0,
            yes_avg_after=_round(yes_avg_after),
            no_avg_after=_round(no_avg_after),
            matched_qty_after=_round(matched_qty) or 0.0,
            matched_pair_cost_after=_round(pair_after),
            matched_edge_after=_round(matched_edge),
            unpaired_side_after=unpaired_side,
            unpaired_qty_after=_round(unpaired_qty) or 0.0,
            fill_role=role,
            projected_pair_cost_before_fill=_round(pair_before),
            projected_pair_cost_after_fill=_round(pair_after_projected),
            orderbook_ts=snap.get("ts") if snap else None,
            orderbook_distance_seconds=snap.get("distance_seconds") if snap else None,
            orderbook_class=ob_class,
            side_bid=_round(side_bid),
            side_ask=_round(side_ask),
            side_bid_depth=_round(side_bid_depth),
            side_ask_depth=_round(side_ask_depth),
            pair_ask_cost=snap.get("pair_ask_cost") if snap else None,
            pair_bid_credit=snap.get("pair_bid_credit") if snap else None,
            btc_price=snap.get("btc_price") if snap else None,
            strike=snap.get("strike") if snap else None,
        )
        fill_contexts.append(ctx)
        per_market_fill_contexts.setdefault(slug, []).append(ctx)

    summaries: list[MarketSummary] = []
    for slug, contexts in per_market_fill_contexts.items():
        yes_qty = sum(c.size for c in contexts if c.side == "YES" and c.action in BUY_ACTIONS)
        yes_notional = sum(c.notional for c in contexts if c.side == "YES" and c.action in BUY_ACTIONS)
        no_qty = sum(c.size for c in contexts if c.side == "NO" and c.action in BUY_ACTIONS)
        no_notional = sum(c.notional for c in contexts if c.side == "NO" and c.action in BUY_ACTIONS)
        yes_avg = _weighted_avg(yes_notional, yes_qty)
        no_avg = _weighted_avg(no_notional, no_qty)
        pair = yes_avg + no_avg if yes_avg is not None and no_avg is not None else None
        matched_qty = min(yes_qty, no_qty)
        edge = matched_qty * (1.0 - pair) if pair is not None else None
        unpaired_side, unpaired_qty = _unpaired(yes_qty, no_qty)
        phase_notional = {"open": 0.0, "mid": 0.0, "late": 0.0, "unknown": 0.0}
        for c in contexts:
            phase_notional[c.phase] = phase_notional.get(c.phase, 0.0) + c.notional
        total_notional = sum(c.notional for c in contexts)
        near_bid = sum(1 for c in contexts if c.orderbook_class == "near_bid_passive")
        near_ask = sum(1 for c in contexts if c.orderbook_class == "near_ask_taker_or_crossed")
        outside = sum(1 for c in contexts if c.orderbook_class == "outside_top_of_book")
        no_book = sum(1 for c in contexts if c.orderbook_class == "no_book")
        pair_ask_values = [float(c.pair_ask_cost) for c in contexts if c.pair_ask_cost is not None]
        slopes = slopes_by_market.get(slug, [])
        tags: list[str] = []
        if pair is not None and pair < 0.95:
            tags.append("sub_095_pair_inventory")
        elif pair is not None and pair < 1.0:
            tags.append("sub_100_pair_inventory")
        if near_bid / len(contexts) >= 0.5:
            tags.append("mostly_passive_bid_fills")
        if phase_notional["open"] / total_notional >= 0.25 if total_notional else False:
            tags.append("meaningful_open_seed")
        if phase_notional["late"] / total_notional >= 0.25 if total_notional else False:
            tags.append("late_repair_or_late_entry")
        if unpaired_side:
            tags.append(f"unpaired_{unpaired_side.lower()}_remainder")

        summaries.append(
            MarketSummary(
                market_slug=slug,
                first_fill_ts=contexts[0].event_ts if contexts else None,
                last_fill_ts=contexts[-1].event_ts if contexts else None,
                first_seconds_after_open=contexts[0].seconds_after_open if contexts else None,
                last_seconds_after_open=contexts[-1].seconds_after_open if contexts else None,
                last_seconds_before_close=contexts[-1].seconds_before_close if contexts else None,
                fill_count=len(contexts),
                total_notional=_round(total_notional) or 0.0,
                yes_qty=_round(yes_qty) or 0.0,
                yes_notional=_round(yes_notional) or 0.0,
                yes_avg=_round(yes_avg),
                no_qty=_round(no_qty) or 0.0,
                no_notional=_round(no_notional) or 0.0,
                no_avg=_round(no_avg),
                matched_qty=_round(matched_qty) or 0.0,
                matched_pair_cost=_round(pair),
                matched_edge_if_held=_round(edge),
                unpaired_side=unpaired_side,
                unpaired_qty=_round(unpaired_qty) or 0.0,
                open_notional=_round(phase_notional["open"]) or 0.0,
                mid_notional=_round(phase_notional["mid"]) or 0.0,
                late_notional=_round(phase_notional["late"]) or 0.0,
                open_share=_round(phase_notional["open"] / total_notional if total_notional else None),
                mid_share=_round(phase_notional["mid"] / total_notional if total_notional else None),
                late_share=_round(phase_notional["late"] / total_notional if total_notional else None),
                near_bid_fills=near_bid,
                near_ask_fills=near_ask,
                outside_book_fills=outside,
                no_book_fills=no_book,
                passive_fill_share=_round(near_bid / len(contexts) if contexts else None),
                avg_pair_ask_cost_at_fills=_round(sum(pair_ask_values) / len(pair_ask_values) if pair_ask_values else None),
                min_pair_ask_cost_at_fills=_round(min(pair_ask_values) if pair_ask_values else None),
                max_abs_btc_slope_60s=_round(max(slopes) if slopes else None),
                strategy_tags=tags,
            )
        )

    summaries.sort(key=lambda s: s.last_fill_ts or "", reverse=True)
    total_matched_qty = sum(s.matched_qty for s in summaries)
    total_edge = sum(s.matched_edge_if_held or 0.0 for s in summaries)
    all_fills = len(fill_contexts)
    coverage = {
        "fills": all_fills,
        "with_orderbook": sum(1 for c in fill_contexts if c.orderbook_class != "no_book"),
        "near_bid_passive": sum(1 for c in fill_contexts if c.orderbook_class == "near_bid_passive"),
        "near_ask_taker_or_crossed": sum(1 for c in fill_contexts if c.orderbook_class == "near_ask_taker_or_crossed"),
        "outside_top_of_book": sum(1 for c in fill_contexts if c.orderbook_class == "outside_top_of_book"),
        "no_book": sum(1 for c in fill_contexts if c.orderbook_class == "no_book"),
    }
    return {
        "wallet_address": wallet.lower(),
        "asset": "btc",
        "interval": "15m",
        "market_count": len(summaries),
        "fill_count": all_fills,
        "paired_markets": sum(1 for s in summaries if s.matched_pair_cost is not None),
        "sub_100_pair_markets": sum(1 for s in summaries if s.matched_pair_cost is not None and s.matched_pair_cost < 1.0),
        "sub_095_pair_markets": sum(1 for s in summaries if s.matched_pair_cost is not None and s.matched_pair_cost < 0.95),
        "total_matched_qty": _round(total_matched_qty),
        "total_matched_edge_if_held": _round(total_edge),
        "orderbook_coverage": coverage,
        "market_summaries": [asdict(s) for s in summaries],
        "fill_contexts": [asdict(c) for c in fill_contexts],
        "safety": "READ_ONLY_RESEARCH: local wallet/event/orderbook analysis only; no private keys, no orders.",
    }


def format_deconstruction_report(report: dict[str, Any], *, max_markets: int = 12, max_fills: int = 60) -> str:
    coverage = report["orderbook_coverage"]
    fill_count = report["fill_count"] or 1
    passive_share = coverage["near_bid_passive"] / fill_count
    lines = [
        "BTC 15m Focus Wallet Strategy Deconstruction — READ-ONLY RESEARCH",
        report["safety"],
        f"Wallet: {report['wallet_address']}",
        f"Scope: {report['asset']} {report['interval']}",
        "",
        "Coverage:",
        f"  markets={report['market_count']} fills={report['fill_count']} paired_markets={report['paired_markets']}",
        f"  sub_1.00_pair_markets={report['sub_100_pair_markets']} sub_0.95_pair_markets={report['sub_095_pair_markets']}",
        f"  total_matched_qty={report['total_matched_qty']} rough_matched_edge_if_held={report['total_matched_edge_if_held']}",
        f"  orderbook: with_book={coverage['with_orderbook']} no_book={coverage['no_book']} near_bid={coverage['near_bid_passive']} near_ask={coverage['near_ask_taker_or_crossed']} outside={coverage['outside_top_of_book']} passive_share={passive_share:.2%}",
        "",
        "Market summaries:",
    ]
    for m in report["market_summaries"][:max_markets]:
        tags = ",".join(m["strategy_tags"]) if m["strategy_tags"] else "-"
        lines.append(
            f"  {m['market_slug']} fills={m['fill_count']} notional={m['total_notional']:.2f} "
            f"pair={m['matched_pair_cost']} edge={m['matched_edge_if_held']} matched={m['matched_qty']} "
            f"YESavg={m['yes_avg']} NOavg={m['no_avg']} unpaired={m['unpaired_side'] or '-'}:{m['unpaired_qty']} "
            f"open/mid/late={m['open_share']}/{m['mid_share']}/{m['late_share']} "
            f"passive={m['passive_fill_share']} tags={tags}"
        )
    lines += ["", "Recent fill contexts:"]
    recent_fills = list(reversed(report["fill_contexts"]))[:max_fills]
    for f in recent_fills:
        lines.append(
            f"  {f['event_ts']} {f['market_slug']} sec={f['seconds_after_open']} {f['phase']} "
            f"{f['side']} {f['action']} px={f['price']} size={f['size']} role={f['fill_role']} "
            f"pair_before={f['projected_pair_cost_before_fill']} pair_after={f['matched_pair_cost_after']} "
            f"matched={f['matched_qty_after']} edge={f['matched_edge_after']} ob={f['orderbook_class']} "
            f"bid/ask={f['side_bid']}/{f['side_ask']}"
        )
    lines += [
        "",
        "Interpretation guardrail: matched_edge_if_held is reconstructed from public fills and assumes matched YES+NO pairs settle to $1. It is not official realized PnL until positions/settlement claims are reconciled.",
    ]
    return "\n".join(lines)


def write_artifacts(report: dict[str, Any], out_dir: str | Path) -> dict[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "btc15m_focus_deconstruction.json"
    markets_path = out / "btc15m_focus_markets.csv"
    fills_path = out / "btc15m_focus_fills.csv"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    markets = report["market_summaries"]
    fills = report["fill_contexts"]
    if markets:
        with markets_path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(markets[0].keys()))
            writer.writeheader()
            writer.writerows(markets)
    else:
        markets_path.write_text("")
    if fills:
        with fills_path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(fills[0].keys()))
            writer.writeheader()
            writer.writerows(fills)
    else:
        fills_path.write_text("")
    return {"json": str(json_path), "markets_csv": str(markets_path), "fills_csv": str(fills_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Deconstruct the focus wallet's BTC 15m Polymarket strategy using local wallet fills and the Polymarket 1s orderbook feed.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--wallet", default=None)
    parser.add_argument("--feed-db", default=None)
    parser.add_argument("--tolerance-seconds", type=float, default=2.0)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--write-artifacts", action="store_true")
    parser.add_argument("--out-dir", default="data/reports")
    parser.add_argument("--max-markets", type=int, default=12)
    parser.add_argument("--max-fills", type=int, default=60)
    args = parser.parse_args()

    cfg = load_config(args.config)
    wallet = args.wallet or cfg.data.get("follow_wallet", {}).get("focus_wallet")
    if not wallet:
        raise SystemExit("No wallet supplied and follow_wallet.focus_wallet is not set in config.")
    conn = connect(cfg.db_path)
    initialize_schema(conn)
    feed_path = args.feed_db or cfg.data.get("polymarket_1s_feed", {}).get("sqlite_path") or cfg.data.get("btc_feed", {}).get("sqlite_path") or str(DEFAULT_POLYMARKET_1S_DB)
    feed_conn = connect_feed(feed_path)
    report = build_btc15m_deconstruction(conn, feed_conn, wallet=wallet, tolerance_seconds=args.tolerance_seconds, limit=args.limit)
    print(format_deconstruction_report(report, max_markets=args.max_markets, max_fills=args.max_fills))
    if args.write_artifacts:
        paths = write_artifacts(report, args.out_dir)
        print("\nArtifacts:")
        for key, path in paths.items():
            print(f"  {key}: {path}")


if __name__ == "__main__":
    main()
