from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import load_config
from .db import connect, initialize_schema, insert_dict
from .util import parse_ts, utc_now_iso
from .adapter_polymarket_1s import connect_feed as connect_polymarket_feed, load_snapshots as load_polymarket_feed_snapshots

BUY_ACTIONS = {"buy", "add"}
SIDES = ("YES", "NO")


@dataclass
class PassivePairBuilderConfig:
    target_pair_cost: float = 0.95
    hard_pair_cost_ceiling: float = 0.99
    max_unpaired_qty: float = 100.0
    max_pair_notional: float = 2000.0
    max_side_notional: float = 1000.0
    max_contract_notional: float = 2000.0
    max_imbalance_ratio: float = 2.0
    stop_new_pairs_seconds_before_close: float = 30.0
    repair_only_seconds_before_close: float = 60.0
    min_visible_liquidity: float = 0.0
    max_spread: float = 0.10
    open_window_seconds: float = 60.0
    repair_window_seconds: float = 60.0
    edge_cents: float = 3.0
    tick: float = 0.01
    order_size: float = 10.0
    starter_max_price: float = 0.47
    max_side_price: float = 0.60
    volatility_mid_move_threshold: float = 0.04
    volatility_realized_threshold: float = 0.04
    quote_stale_seconds: float = 5.0
    touch_fill_mode: str = "ask_touch"
    paper_only: bool = True
    dry_run: bool = False


@dataclass
class Snapshot:
    market_slug: str | None
    market_id: str | None
    token_id: str | None
    ts: str
    seconds_after_open: float | None = None
    seconds_to_close: float | None = None
    yes_bid: float | None = None
    yes_ask: float | None = None
    yes_mid: float | None = None
    no_bid: float | None = None
    no_ask: float | None = None
    no_mid: float | None = None
    top_bid_size: float | None = None
    top_ask_size: float | None = None
    top_of_book_liquidity: float | None = None
    realized_vol_60s: float | None = None
    slope_15s: float | None = None
    atr_proxy: float | None = None


@dataclass
class Inventory:
    yes_qty: float = 0.0
    no_qty: float = 0.0
    yes_notional: float = 0.0
    no_notional: float = 0.0

    @property
    def yes_avg_entry(self) -> float | None:
        return round(self.yes_notional / self.yes_qty, 4) if self.yes_qty else None

    @property
    def no_avg_entry(self) -> float | None:
        return round(self.no_notional / self.no_qty, 4) if self.no_qty else None

    @property
    def matched_pair_qty(self) -> float:
        return round(min(self.yes_qty, self.no_qty), 4)

    @property
    def matched_pair_cost(self) -> float | None:
        if self.yes_avg_entry is None or self.no_avg_entry is None:
            return None
        return round(self.yes_avg_entry + self.no_avg_entry, 4)

    @property
    def locked_edge_per_pair(self) -> float | None:
        if self.matched_pair_cost is None:
            return None
        return round(1.0 - self.matched_pair_cost, 4)

    @property
    def locked_profit_if_held(self) -> float:
        edge = self.locked_edge_per_pair
        if edge is None:
            return 0.0
        return round(self.matched_pair_qty * max(0.0, edge), 4)

    @property
    def unpaired_yes_qty(self) -> float:
        return round(max(0.0, self.yes_qty - self.no_qty), 4)

    @property
    def unpaired_no_qty(self) -> float:
        return round(max(0.0, self.no_qty - self.yes_qty), 4)

    @property
    def unpaired_side(self) -> str | None:
        if self.unpaired_yes_qty > 0:
            return "YES"
        if self.unpaired_no_qty > 0:
            return "NO"
        return None

    @property
    def unpaired_qty(self) -> float:
        return round(max(self.unpaired_yes_qty, self.unpaired_no_qty), 4)

    @property
    def imbalance_ratio(self) -> float:
        smaller = min(self.yes_qty, self.no_qty)
        larger = max(self.yes_qty, self.no_qty)
        if larger <= 0:
            return 0.0
        if smaller <= 0:
            return round(larger, 4)
        return round(larger / smaller, 4)

    @property
    def total_notional(self) -> float:
        return round(self.yes_notional + self.no_notional, 4)

    def add_fill(self, side: str, qty: float, price: float) -> None:
        if side == "YES":
            self.yes_qty += qty
            self.yes_notional += qty * price
        elif side == "NO":
            self.no_qty += qty
            self.no_notional += qty * price
        else:
            raise ValueError(f"unknown side {side}")

    def projected_avg(self, side: str, qty: float, price: float) -> float | None:
        if side == "YES":
            return weighted_average_entry(self.yes_qty, self.yes_avg_entry, qty, price)
        return weighted_average_entry(self.no_qty, self.no_avg_entry, qty, price)

    def projected_pair_cost(self, side: str, qty: float, price: float) -> float | None:
        if side == "YES":
            yes_avg = self.projected_avg("YES", qty, price)
            no_avg = self.no_avg_entry
        else:
            yes_avg = self.yes_avg_entry
            no_avg = self.projected_avg("NO", qty, price)
        if yes_avg is None or no_avg is None:
            return None
        return round(yes_avg + no_avg, 4)


def weighted_average_entry(existing_qty: float, existing_avg: float | None, new_qty: float, new_price: float) -> float | None:
    total = float(existing_qty or 0) + float(new_qty or 0)
    if total <= 0:
        return None
    existing_notional = float(existing_qty or 0) * float(existing_avg or 0)
    return round((existing_notional + float(new_qty or 0) * float(new_price)) / total, 4)


@dataclass
class RestingQuote:
    market_slug: str | None
    market_id: str | None
    side: str
    quote_price: float
    size: float
    placed_ts: str
    last_seen_ts: str
    status: str = "active"


@dataclass
class QuotingMetrics:
    quotes_placed: int = 0
    quotes_cancelled: int = 0
    quote_touches: int = 0
    simulated_fills: int = 0
    seconds_spent_quoting: int = 0
    missed_fill_estimate: int = 0
    max_imbalance: float = 0.0
    reached_50: bool = False
    reached_200: bool = False
    reached_1000: bool = False
    pair_costs_after_fills: list[float] = field(default_factory=list)


def _max_acceptable_price(side: str, inv: Inventory, cfg: PassivePairBuilderConfig, *, repair_only: bool = False) -> float:
    ceiling = cfg.hard_pair_cost_ceiling if repair_only else cfg.target_pair_cost
    if side == "YES":
        if inv.no_avg_entry is None:
            return cfg.starter_max_price
        return max(0.0, round(ceiling - inv.no_avg_entry, 4))
    if inv.yes_avg_entry is None:
        return cfg.starter_max_price
    return max(0.0, round(ceiling - inv.yes_avg_entry, 4))


def _compute_passive_quote(snapshot: Snapshot, side: str, inv: Inventory, cfg: PassivePairBuilderConfig, *, repair_only: bool = False) -> tuple[float | None, float | None, float | None, float | None, float]:
    quote, bid, ask, mid = _quote_for_side(snapshot, side, cfg)
    max_price = _max_acceptable_price(side, inv, cfg, repair_only=repair_only)
    if quote is None:
        return None, bid, ask, mid, max_price
    quote = min(quote, max_price)
    if ask is not None and quote >= ask:
        quote = ask - cfg.tick
    quote = round(max(0.01, quote), 4)
    if quote > max_price or quote <= 0:
        return None, bid, ask, mid, max_price
    return quote, bid, ask, mid, max_price


def _quote_touched(snapshot: Snapshot, quote: RestingQuote) -> bool:
    # Conservative paper rule: a passive bid fills only when later visible ask
    # touches/trades through the resting quote. This avoids same-tick refills and
    # does not model queue priority as guaranteed.
    ask = snapshot.yes_ask if quote.side == "YES" else snapshot.no_ask
    if ask is None:
        return False
    return ask <= quote.quote_price


def _quote_stale(snapshot: Snapshot, quote: RestingQuote, cfg: PassivePairBuilderConfig) -> bool:
    placed = parse_ts(quote.placed_ts)
    now = parse_ts(snapshot.ts)
    if not placed or not now:
        return False
    return (now - placed).total_seconds() >= cfg.quote_stale_seconds


def _upsert_active_quote(conn: sqlite3.Connection, quote: RestingQuote, *, dry_run: bool = False) -> None:
    if dry_run:
        return
    conn.execute(
        """
        INSERT INTO paper_pair_builder_active_quotes(market_slug, market_id, side, quote_price, size, placed_ts, last_seen_ts, status)
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (quote.market_slug, quote.market_id, quote.side, quote.quote_price, quote.size, quote.placed_ts, quote.last_seen_ts, quote.status),
    )
    conn.commit()


def _quote_for_side(snapshot: Snapshot, side: str, cfg: PassivePairBuilderConfig) -> tuple[float | None, float | None, float | None, float | None]:
    if side == "YES":
        bid, ask, mid = snapshot.yes_bid, snapshot.yes_ask, snapshot.yes_mid
    else:
        bid, ask, mid = snapshot.no_bid, snapshot.no_ask, snapshot.no_mid
    if bid is None or ask is None:
        return None, bid, ask, mid
    fair = mid if mid is not None else (bid + ask) / 2
    quote = min(bid + cfg.tick, fair - cfg.edge_cents / 100.0)
    quote = round(max(0.01, quote), 4)
    if quote >= ask:
        quote = round(ask - cfg.tick, 4)
    return quote if quote > 0 else None, bid, ask, mid


def _visible_liquidity(snapshot: Snapshot) -> float | None:
    if snapshot.top_of_book_liquidity is not None:
        return snapshot.top_of_book_liquidity
    if snapshot.top_bid_size is not None or snapshot.top_ask_size is not None:
        return float(snapshot.top_bid_size or 0) + float(snapshot.top_ask_size or 0)
    return None


def _side_spread(snapshot: Snapshot, side: str) -> float | None:
    bid = snapshot.yes_bid if side == "YES" else snapshot.no_bid
    ask = snapshot.yes_ask if side == "YES" else snapshot.no_ask
    if bid is None or ask is None:
        return None
    return ask - bid


def _is_volatility_spike(snapshot: Snapshot, prev: Snapshot | None, cfg: PassivePairBuilderConfig) -> bool:
    if snapshot.realized_vol_60s is not None and snapshot.realized_vol_60s >= cfg.volatility_realized_threshold:
        return True
    if snapshot.slope_15s is not None and abs(snapshot.slope_15s) >= cfg.volatility_mid_move_threshold:
        return True
    if snapshot.atr_proxy is not None and abs(snapshot.atr_proxy) >= cfg.volatility_mid_move_threshold:
        return True
    if prev and snapshot.yes_mid is not None and prev.yes_mid is not None and abs(snapshot.yes_mid - prev.yes_mid) >= cfg.volatility_mid_move_threshold:
        return True
    if prev and snapshot.no_mid is not None and prev.no_mid is not None and abs(snapshot.no_mid - prev.no_mid) >= cfg.volatility_mid_move_threshold:
        return True
    return False


def _decision_base(snapshot: Snapshot, side: str, decision: str, quote_price: float | None, bid: float | None, ask: float | None, mid: float | None, size: float, reason: str, projected_pair_cost: float | None, inv: Inventory, tags: list[str]) -> dict[str, Any]:
    return {
        "market_slug": snapshot.market_slug,
        "market_id": snapshot.market_id,
        "token_id": snapshot.token_id,
        "ts": snapshot.ts,
        "seconds_after_open": snapshot.seconds_after_open,
        "seconds_to_close": snapshot.seconds_to_close,
        "side": side,
        "decision": decision,
        "quote_price": quote_price,
        "reference_bid": bid,
        "reference_ask": ask,
        "reference_mid": mid,
        "size": size,
        "reason": reason,
        "projected_pair_cost": projected_pair_cost,
        "yes_qty": round(inv.yes_qty, 4),
        "no_qty": round(inv.no_qty, 4),
        "yes_avg_entry": inv.yes_avg_entry,
        "no_avg_entry": inv.no_avg_entry,
        "matched_pair_qty": inv.matched_pair_qty,
        "matched_pair_cost": inv.matched_pair_cost,
        "locked_profit_if_held": inv.locked_profit_if_held,
        "unpaired_side": inv.unpaired_side,
        "unpaired_qty": inv.unpaired_qty,
        "imbalance_ratio": inv.imbalance_ratio,
        "tags": json.dumps(tags, sort_keys=True),
    }


def _persist_decision(conn: sqlite3.Connection, row: dict[str, Any], dry_run: bool) -> None:
    # Dry-run means no live/order side effects. Persisting local paper decisions is
    # still useful for reports, and this table is explicitly read-only research.
    insert_dict(conn, "paper_pair_builder_decisions", row)


def _risk_skip_reason(side: str, price: float, size: float, inv: Inventory, snapshot: Snapshot, cfg: PassivePairBuilderConfig, repair_only: bool, volatility: bool) -> str | None:
    if _side_spread(snapshot, side) is None:
        return "missing_orderbook_snapshot"
    if (_side_spread(snapshot, side) or 0) > cfg.max_spread:
        return "spread_too_wide"
    liq = _visible_liquidity(snapshot)
    if liq is not None and liq < cfg.min_visible_liquidity:
        return "insufficient_visible_liquidity"
    if price > cfg.max_side_price and not repair_only:
        return "above_max_side_price"

    projected_yes_qty = inv.yes_qty + (size if side == "YES" else 0)
    projected_no_qty = inv.no_qty + (size if side == "NO" else 0)
    projected_yes_notional = inv.yes_notional + (size * price if side == "YES" else 0)
    projected_no_notional = inv.no_notional + (size * price if side == "NO" else 0)
    projected_total_notional = projected_yes_notional + projected_no_notional
    projected_unpaired = abs(projected_yes_qty - projected_no_qty)

    if projected_unpaired > cfg.max_unpaired_qty:
        return "max_unpaired_qty"
    if (projected_yes_notional if side == "YES" else projected_no_notional) > cfg.max_side_notional:
        return "max_side_notional"
    if projected_total_notional > cfg.max_contract_notional:
        return "max_contract_notional"
    projected_pair_qty = min(projected_yes_qty, projected_no_qty)
    if projected_pair_qty:
        # Pair notional is the capital warehoused in completed YES+NO bundles, not
        # just qty * a configured target. This keeps the $2000/contract cap honest.
        projected_yes_avg = projected_yes_notional / projected_yes_qty if projected_yes_qty else None
        projected_no_avg = projected_no_notional / projected_no_qty if projected_no_qty else None
        if projected_yes_avg is not None and projected_no_avg is not None:
            projected_pair_notional = projected_pair_qty * (projected_yes_avg + projected_no_avg)
            if projected_pair_notional > cfg.max_pair_notional:
                return "max_pair_notional"
    projected_ratio = _projected_imbalance_ratio(inv, side, size)
    if projected_ratio > cfg.max_imbalance_ratio and min(projected_yes_qty, projected_no_qty) > 0:
        return "max_imbalance_ratio"

    has_opposite = (inv.no_qty > 0 if side == "YES" else inv.yes_qty > 0)
    if not has_opposite:
        near_open = (snapshot.seconds_after_open or 0) <= cfg.open_window_seconds
        if not near_open and not volatility:
            return "starter_blocked_outside_open_without_volatility"
        if price > cfg.starter_max_price:
            return "starter_above_max_price"
    projected_pair_cost = inv.projected_pair_cost(side, size, price)
    if projected_pair_cost is not None:
        limit = cfg.hard_pair_cost_ceiling if repair_only else cfg.target_pair_cost
        if projected_pair_cost > limit:
            return "projected_pair_cost_too_high"
    return None


def _projected_imbalance_ratio(inv: Inventory, side: str, size: float) -> float:
    yes = inv.yes_qty + (size if side == "YES" else 0)
    no = inv.no_qty + (size if side == "NO" else 0)
    smaller = min(yes, no)
    larger = max(yes, no)
    if larger <= 0:
        return 0.0
    if smaller <= 0:
        return larger
    return larger / smaller


def simulate_snapshots(conn: sqlite3.Connection, snapshots: list[Snapshot], cfg: PassivePairBuilderConfig, *, inventory: Inventory | None = None) -> list[dict[str, Any]]:
    inv = inventory or Inventory()
    out: list[dict[str, Any]] = []
    prev: Snapshot | None = None
    for snapshot in sorted(snapshots, key=lambda s: s.ts):
        volatility = _is_volatility_spike(snapshot, prev, cfg)
        repair_only = snapshot.seconds_to_close is not None and snapshot.seconds_to_close <= cfg.repair_only_seconds_before_close
        stop_new = snapshot.seconds_to_close is not None and snapshot.seconds_to_close <= cfg.stop_new_pairs_seconds_before_close
        for side in SIDES:
            quote, bid, ask, mid = _quote_for_side(snapshot, side, cfg)
            tags: list[str] = []
            if snapshot.seconds_after_open is not None and snapshot.seconds_after_open <= cfg.open_window_seconds:
                tags.append("early_open_liquidity")
            if volatility:
                tags.append("volatility_spike")
            smaller_side = "YES" if inv.yes_qty < inv.no_qty else "NO" if inv.no_qty < inv.yes_qty else None
            if repair_only:
                tags.append("inventory_repair")
                if smaller_side and side != smaller_side:
                    row = _decision_base(snapshot, side, "skip", quote, bid, ask, mid, 0, "repair_only_smaller_side", None, inv, tags)
                    _persist_decision(conn, row, cfg.dry_run)
                    out.append(row)
                    continue
            elif smaller_side and side != smaller_side:
                row = _decision_base(snapshot, side, "skip", quote, bid, ask, mid, 0, "prefer_underweight_side", None, inv, tags)
                _persist_decision(conn, row, cfg.dry_run)
                out.append(row)
                continue
            elif stop_new and smaller_side is None:
                row = _decision_base(snapshot, side, "skip", quote, bid, ask, mid, 0, "stop_new_pairs_before_close", None, inv, tags)
                _persist_decision(conn, row, cfg.dry_run)
                out.append(row)
                continue
            if quote is None:
                row = _decision_base(snapshot, side, "skip", quote, bid, ask, mid, 0, "missing_orderbook_snapshot", None, inv, tags)
                _persist_decision(conn, row, cfg.dry_run)
                out.append(row)
                continue
            size = cfg.order_size
            projected_pair_cost = inv.projected_pair_cost(side, size, quote)
            reason = _risk_skip_reason(side, quote, size, inv, snapshot, cfg, repair_only, volatility)
            if reason:
                row = _decision_base(snapshot, side, "skip", quote, bid, ask, mid, 0, reason, projected_pair_cost, inv, tags)
                _persist_decision(conn, row, cfg.dry_run)
                out.append(row)
                continue
            quote_row = _decision_base(snapshot, side, "repair" if repair_only else "quote", quote, bid, ask, mid, size, "risk_checks_passed", projected_pair_cost, inv, tags)
            _persist_decision(conn, quote_row, cfg.dry_run)
            out.append(quote_row)
            inv.add_fill(side, size, quote)
            fill_row = _decision_base(snapshot, side, "fill_simulated", quote, bid, ask, mid, size, "paper_touch_fill_assumption", inv.matched_pair_cost, inv, tags)
            _persist_decision(conn, fill_row, cfg.dry_run)
            out.append(fill_row)
        prev = snapshot
    return out


def simulate_resting_quote_snapshots(conn: sqlite3.Connection, snapshots: list[Snapshot], cfg: PassivePairBuilderConfig, *, inventory: Inventory | None = None) -> dict[str, Any]:
    inv = inventory or Inventory()
    active: dict[str, RestingQuote] = {}
    events: list[dict[str, Any]] = []
    metrics = QuotingMetrics()
    prev_market: str | None = None
    prev: Snapshot | None = None
    for snapshot in sorted(snapshots, key=lambda s: s.ts):
        market_key = snapshot.market_slug or snapshot.market_id or "unknown"
        if prev_market is not None and market_key != prev_market:
            active.clear()
            inv = Inventory()
        prev_market = market_key
        volatility = _is_volatility_spike(snapshot, prev, cfg)
        repair_only = snapshot.seconds_to_close is not None and snapshot.seconds_to_close <= cfg.repair_only_seconds_before_close
        stop_new = snapshot.seconds_to_close is not None and snapshot.seconds_to_close <= cfg.stop_new_pairs_seconds_before_close

        for side, quote in list(active.items()):
            quote.last_seen_ts = snapshot.ts
            if _quote_touched(snapshot, quote):
                metrics.quote_touches += 1
                inv.add_fill(side, quote.size, quote.quote_price)
                metrics.simulated_fills += 1
                metrics.max_imbalance = max(metrics.max_imbalance, inv.imbalance_ratio)
                if inv.matched_pair_cost is not None:
                    metrics.pair_costs_after_fills.append(inv.matched_pair_cost)
                metrics.reached_50 = metrics.reached_50 or inv.matched_pair_qty * (inv.matched_pair_cost or 0) >= 50
                metrics.reached_200 = metrics.reached_200 or inv.matched_pair_qty * (inv.matched_pair_cost or 0) >= 200
                metrics.reached_1000 = metrics.reached_1000 or inv.matched_pair_qty * (inv.matched_pair_cost or 0) >= 1000
                row = _decision_base(snapshot, side, "fill_simulated", quote.quote_price, None, snapshot.yes_ask if side == "YES" else snapshot.no_ask, None, quote.size, "resting_quote_touched_later_snapshot", inv.matched_pair_cost, inv, ["resting_quote", "passive_fill"])
                _persist_decision(conn, row, cfg.dry_run)
                events.append(row)
                active.pop(side, None)
            elif _quote_stale(snapshot, quote, cfg):
                row = _decision_base(snapshot, side, "cancel", quote.quote_price, None, snapshot.yes_ask if side == "YES" else snapshot.no_ask, None, 0, "quote_stale_cancel_replace", inv.projected_pair_cost(side, quote.size, quote.quote_price), inv, ["resting_quote"])
                _persist_decision(conn, row, cfg.dry_run)
                events.append(row)
                active.pop(side, None)
                metrics.quotes_cancelled += 1
            else:
                metrics.seconds_spent_quoting += 1

        if stop_new and not repair_only:
            prev = snapshot
            continue

        smaller_side = "YES" if inv.yes_qty < inv.no_qty else "NO" if inv.no_qty < inv.yes_qty else None
        for side in SIDES:
            if side in active:
                continue
            tags = ["resting_quote"]
            if snapshot.seconds_after_open is not None and snapshot.seconds_after_open <= cfg.open_window_seconds:
                tags.append("early_open_liquidity")
            if volatility:
                tags.append("volatility_spike")
            if repair_only:
                tags.append("inventory_repair")
                if smaller_side and side != smaller_side:
                    row = _decision_base(snapshot, side, "skip", None, None, None, None, 0, "repair_only_smaller_side", None, inv, tags)
                    _persist_decision(conn, row, cfg.dry_run)
                    events.append(row)
                    continue
            elif smaller_side and side != smaller_side and not ((snapshot.seconds_after_open or 999999) <= cfg.open_window_seconds):
                row = _decision_base(snapshot, side, "skip", None, None, None, None, 0, "prefer_underweight_side", None, inv, tags)
                _persist_decision(conn, row, cfg.dry_run)
                events.append(row)
                continue

            quote_price, bid, ask, mid, max_price = _compute_passive_quote(snapshot, side, inv, cfg, repair_only=repair_only)
            if quote_price is None:
                row = _decision_base(snapshot, side, "skip", quote_price, bid, ask, mid, 0, "no_valid_passive_quote", None, inv, tags + ["max_price", str(max_price)])
                _persist_decision(conn, row, cfg.dry_run)
                events.append(row)
                continue
            reason = _risk_skip_reason(side, quote_price, cfg.order_size, inv, snapshot, cfg, repair_only, volatility)
            projected_pair_cost = inv.projected_pair_cost(side, cfg.order_size, quote_price)
            if reason:
                row = _decision_base(snapshot, side, "skip", quote_price, bid, ask, mid, 0, reason, projected_pair_cost, inv, tags)
                _persist_decision(conn, row, cfg.dry_run)
                events.append(row)
                continue
            quote = RestingQuote(snapshot.market_slug, snapshot.market_id, side, quote_price, cfg.order_size, snapshot.ts, snapshot.ts)
            active[side] = quote
            _upsert_active_quote(conn, quote, dry_run=cfg.dry_run)
            metrics.quotes_placed += 1
            row = _decision_base(snapshot, side, "quote", quote_price, bid, ask, mid, cfg.order_size, "resting_passive_bid_placed", projected_pair_cost, inv, tags)
            _persist_decision(conn, row, cfg.dry_run)
            events.append(row)
        prev = snapshot
    return {"events": events, "inventory": inv, "active_quotes": active, "metrics": metrics}


def _safe_avg(qty: float, notional: float) -> float | None:
    return round(notional / qty, 4) if qty else None


def _wallet_inventory_from_events(conn: sqlite3.Connection, *, wallet: str, market_slug: str) -> tuple[Inventory, str | None]:
    rows = conn.execute(
        """
        SELECT event_ts, side, action, price, size
        FROM wallet_events
        WHERE wallet_address=? AND market_slug=? AND action IN ('buy','add') AND side IN ('YES','NO')
        ORDER BY event_ts ASC, id ASC
        """,
        (wallet, market_slug),
    ).fetchall()
    inv = Inventory()
    first_ts = None
    for r in rows:
        if r["price"] is None or r["size"] is None:
            continue
        first_ts = first_ts or r["event_ts"]
        inv.add_fill(r["side"], float(r["size"]), float(r["price"]))
    return inv, first_ts


def compare_wallet_to_paper(conn: sqlite3.Connection, *, wallet: str, market_slug: str, inventory: Inventory, first_fill_ts: str | None = None) -> dict[str, Any]:
    wallet_inv, wallet_first_ts = _wallet_inventory_from_events(conn, wallet=wallet, market_slug=market_slug)
    timing_diff = None
    if wallet_first_ts and first_fill_ts:
        a, b = parse_ts(wallet_first_ts), parse_ts(first_fill_ts)
        if a and b:
            timing_diff = abs((b - a).total_seconds())
    pair_delta = abs((wallet_inv.matched_pair_cost or 1) - (inventory.matched_pair_cost or 1))
    qty_base = max(wallet_inv.matched_pair_qty, inventory.matched_pair_qty, 1)
    qty_delta = abs(wallet_inv.matched_pair_qty - inventory.matched_pair_qty) / qty_base
    imbalance_match = 0 if wallet_inv.unpaired_side == inventory.unpaired_side else 0.15
    timing_penalty = min((timing_diff or 0) / 300, 0.2)
    score = max(0.0, 1.0 - pair_delta * 4 - qty_delta * 0.4 - imbalance_match - timing_penalty)
    return {
        "wallet_yes_avg": wallet_inv.yes_avg_entry,
        "paper_yes_avg": inventory.yes_avg_entry,
        "wallet_no_avg": wallet_inv.no_avg_entry,
        "paper_no_avg": inventory.no_avg_entry,
        "wallet_pair_cost": wallet_inv.matched_pair_cost,
        "paper_pair_cost": inventory.matched_pair_cost,
        "wallet_matched_pair_qty": wallet_inv.matched_pair_qty,
        "paper_matched_pair_qty": inventory.matched_pair_qty,
        "wallet_unpaired_side": wallet_inv.unpaired_side,
        "paper_unpaired_side": inventory.unpaired_side,
        "wallet_unpaired_qty": wallet_inv.unpaired_qty,
        "paper_unpaired_qty": inventory.unpaired_qty,
        "timing_difference_seconds": timing_diff,
        "similarity_score": round(score, 4),
    }


def _since_cutoff(since: str | None) -> str | None:
    if not since:
        return None
    text = since.strip().lower()
    if text.endswith("h") and text[:-1].isdigit():
        return (datetime.now(timezone.utc) - timedelta(hours=int(text[:-1]))).isoformat()
    return since


def _infer_open_close(market_slug: str | None, market: sqlite3.Row | None = None) -> tuple[datetime | None, datetime | None]:
    start = parse_ts(market["start_ts"]) if market and "start_ts" in market.keys() else None
    close = parse_ts(market["close_ts"]) if market and "close_ts" in market.keys() else None
    if not start and market_slug:
        m = re.search(r"-(\d{10})(?:\D|$)", market_slug)
        if m:
            start = datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
            window = 300 if "5m" in market_slug else 900 if "15m" in market_slug else None
            close = start + timedelta(seconds=window) if window else close
    return start, close


def _snapshot_from_polymarket_feed(snap: dict[str, Any]) -> Snapshot:
    yes_mid = (snap["yes_bid"] + snap["yes_ask"]) / 2 if snap.get("yes_bid") is not None and snap.get("yes_ask") is not None else None
    no_mid = (snap["no_bid"] + snap["no_ask"]) / 2 if snap.get("no_bid") is not None and snap.get("no_ask") is not None else None
    open_ts = parse_ts(snap.get("market_open_time"))
    ts = parse_ts(snap.get("ts"))
    seconds_after_open = (ts - open_ts).total_seconds() if ts and open_ts else None
    return Snapshot(
        market_slug=snap.get("market_slug") or snap.get("market_key"),
        market_id=snap.get("condition_id"),
        token_id=snap.get("yes_token_id"),
        ts=snap.get("ts"),
        seconds_after_open=seconds_after_open,
        seconds_to_close=snap.get("seconds_to_close"),
        yes_bid=snap.get("yes_bid"),
        yes_ask=snap.get("yes_ask"),
        yes_mid=yes_mid,
        no_bid=snap.get("no_bid"),
        no_ask=snap.get("no_ask"),
        no_mid=no_mid,
        top_bid_size=(snap.get("yes_bid_depth") or 0) + (snap.get("no_bid_depth") or 0),
        top_ask_size=(snap.get("yes_ask_depth") or 0) + (snap.get("no_ask_depth") or 0),
        top_of_book_liquidity=sum(float(snap.get(k) or 0) for k in ("yes_bid_depth", "yes_ask_depth", "no_bid_depth", "no_ask_depth")),
        slope_15s=None,
    )


def load_polymarket_1s_snapshots(feed_db: str, *, market_slug: str | None = None, since: str | None = "24h", limit: int | None = None) -> list[Snapshot]:
    conn = connect_polymarket_feed(feed_db)
    try:
        snapshots = load_polymarket_feed_snapshots(conn, market_key=market_slug, since=_since_cutoff(since), limit=limit)
    finally:
        conn.close()
    return [_snapshot_from_polymarket_feed(s) for s in snapshots]


def load_snapshots(conn: sqlite3.Connection, *, market_slug: str | None = None, asset: str = "btc", interval: str = "5m", since: str | None = "24h") -> list[Snapshot]:
    cutoff = _since_cutoff(since)
    params: list[Any] = []
    where = []
    if cutoff:
        where.append("ms.snapshot_ts >= ?")
        params.append(cutoff)
    if market_slug:
        where.append("(m.slug=? OR ms.market_id=? OR ms.condition_id=?)")
        params.extend([market_slug, market_slug, market_slug])
    else:
        where.append("LOWER(COALESCE(m.slug, '')) LIKE ?")
        params.append(f"%{asset.lower()}%{interval.lower()}%")
    sql_where = "WHERE " + " AND ".join(where) if where else ""
    rows = conn.execute(
        f"""
        SELECT ms.*, m.slug AS market_slug, m.start_ts, m.close_ts
        FROM market_snapshots ms
        LEFT JOIN markets m ON m.market_id=ms.market_id OR m.condition_id=ms.condition_id
        {sql_where}
        ORDER BY ms.snapshot_ts ASC, ms.id ASC
        """,
        tuple(params),
    ).fetchall()
    out: list[Snapshot] = []
    for r in rows:
        slug = r["market_slug"] or market_slug
        opened, closed = _infer_open_close(slug, r)
        ts = parse_ts(r["snapshot_ts"])
        seconds_after_open = (ts - opened).total_seconds() if ts and opened else None
        seconds_to_close = (closed - ts).total_seconds() if ts and closed else None
        out.append(Snapshot(
            market_slug=slug,
            market_id=r["market_id"],
            token_id=r["token_id"],
            ts=r["snapshot_ts"],
            seconds_after_open=seconds_after_open,
            seconds_to_close=seconds_to_close,
            yes_bid=r["yes_bid"],
            yes_ask=r["yes_ask"],
            yes_mid=r["yes_mid"],
            no_bid=r["no_bid"],
            no_ask=r["no_ask"],
            no_mid=r["no_mid"],
            top_bid_size=r["top_bid_size"],
            top_ask_size=r["top_ask_size"],
            top_of_book_liquidity=r["top_of_book_liquidity"],
        ))
    return out


def format_passive_pair_builder_report(conn: sqlite3.Connection, *, market_slug: str | None = None, wallet: str | None = None) -> str:
    params: tuple[Any, ...] = (market_slug,) if market_slug else ()
    where = "WHERE market_slug=?" if market_slug else ""
    rows = conn.execute(f"SELECT * FROM paper_pair_builder_decisions {where} ORDER BY ts ASC, id ASC", params).fetchall()
    lines = [
        "Passive Pair Builder Report — READ-ONLY/PAPER-ONLY",
        "Safety: local SQLite decisions only; no private keys, no signers, no orders, no live execution.",
        "Note: this research simulator does not claim profitability.",
        f"Rows: {len(rows)}",
        "",
    ]
    if not rows:
        lines.append("No paper_pair_builder_decisions rows. If orderbook snapshots are missing, quote simulation cannot classify execution quality.")
        return "\n".join(lines)
    markets = sorted({r["market_slug"] or r["market_id"] or "unknown" for r in rows})
    for key in markets:
        mrows = [r for r in rows if (r["market_slug"] or r["market_id"] or "unknown") == key]
        fills = [r for r in mrows if r["decision"] == "fill_simulated"]
        latest = fills[-1] if fills else mrows[-1]
        skipped: dict[str, int] = {}
        for r in mrows:
            if r["decision"] == "skip":
                skipped[r["reason"] or "unknown"] = skipped.get(r["reason"] or "unknown", 0) + 1
        open_count = sum(1 for r in mrows if "early_open_liquidity" in (r["tags"] or ""))
        vol_count = sum(1 for r in mrows if "volatility_spike" in (r["tags"] or ""))
        repair_count = sum(1 for r in mrows if "inventory_repair" in (r["tags"] or ""))
        lines.extend([
            f"Market window: {key}",
            f"- total quotes placed: {sum(1 for r in mrows if r['decision'] == 'quote')}",
            f"- simulated fills: {len(fills)}",
            f"- quote cancellations: {sum(1 for r in mrows if r['decision'] == 'cancel')}",
            f"- first quote time: {_first_ts(mrows, 'quote')}",
            f"- first simulated fill time: {_first_ts(mrows, 'fill_simulated')}",
            f"- YES qty / avg / notional: {latest['yes_qty']} / {latest['yes_avg_entry']} / {_side_notional_estimate(latest, 'YES')}",
            f"- NO qty / avg / notional: {latest['no_qty']} / {latest['no_avg_entry']} / {_side_notional_estimate(latest, 'NO')}",
            f"- matched pair qty: {latest['matched_pair_qty']}",
            f"- matched pair cost: {latest['matched_pair_cost']}",
            f"- locked edge: {None if latest['matched_pair_cost'] is None else round(1 - float(latest['matched_pair_cost']), 4)}",
            f"- locked profit if held: {latest['locked_profit_if_held']}",
            f"- unpaired side and qty: {latest['unpaired_side']} {latest['unpaired_qty']}",
            f"- max imbalance: {max((r['imbalance_ratio'] or 0) for r in mrows)}",
            f"- reached matched notional: $50={_reached_notional(mrows, 50)} $200={_reached_notional(mrows, 200)} $1000={_reached_notional(mrows, 1000)}",
            f"- open-phase decisions: {open_count}",
            f"- volatility-spike decisions: {vol_count}",
            f"- repair decisions: {repair_count}",
            f"- skipped decisions by reason: {skipped or {}}",
        ])
        if wallet:
            inv = Inventory(float(latest["yes_qty"] or 0), float(latest["no_qty"] or 0), _side_notional_estimate(latest, "YES"), _side_notional_estimate(latest, "NO"))
            comparison = compare_wallet_to_paper(conn, wallet=wallet, market_slug=key, inventory=inv, first_fill_ts=_first_ts(mrows, "fill_simulated"))
            lines.append(f"- comparison to watched wallet {wallet}: {json.dumps(comparison, sort_keys=True)}")
        lines.append("")
    return "\n".join(lines)


def _reached_notional(rows: list[sqlite3.Row], dollars: float) -> bool:
    for row in rows:
        cost = row["matched_pair_cost"]
        qty = row["matched_pair_qty"]
        if cost is not None and qty is not None and float(cost) * float(qty) >= dollars:
            return True
    return False


def _first_ts(rows: list[sqlite3.Row], decision: str) -> str | None:
    for r in rows:
        if r["decision"] == decision or (decision == "quote" and r["decision"] in {"quote", "repair"}):
            return r["ts"]
    return None


def _side_notional_estimate(row: sqlite3.Row, side: str) -> float:
    qty = row["yes_qty"] if side == "YES" else row["no_qty"]
    avg = row["yes_avg_entry"] if side == "YES" else row["no_avg_entry"]
    return round(float(qty or 0) * float(avg or 0), 4)


def insert_missing_snapshot_notice(conn: sqlite3.Connection, *, market_slug: str | None, market_id: str | None = None, dry_run: bool = False) -> None:
    row = _decision_base(
        Snapshot(market_slug=market_slug, market_id=market_id, token_id=None, ts=utc_now_iso()),
        "UNKNOWN",
        "skip",
        None,
        None,
        None,
        None,
        0,
        "missing_orderbook_snapshots_quote_simulation_unavailable",
        None,
        Inventory(),
        ["quote_simulation_unavailable"],
    )
    if not dry_run:
        insert_dict(conn, "paper_pair_builder_decisions", row)


def run_from_db(conn: sqlite3.Connection, cfg: PassivePairBuilderConfig, *, market: str | None = None, wallet: str | None = None, asset: str = "btc", interval: str = "5m", since: str = "24h", venue: str = "polymarket", polymarket_feed_db: str | None = None) -> str:
    all_snapshots: list[Snapshot] = []
    missing_sources: list[str] = []
    feed_has_rows = False
    if venue in {"polymarket", "both"} and polymarket_feed_db:
        feed_snapshots = load_polymarket_1s_snapshots(polymarket_feed_db, market_slug=market, since=since)
        if market is None and wallet:
            wallet_markets = {
                r["market_slug"]
                for r in conn.execute(
                    "SELECT DISTINCT market_slug FROM wallet_events WHERE LOWER(wallet_address)=LOWER(?) AND market_slug IS NOT NULL AND LOWER(market_slug) LIKE ?",
                    (wallet, f"{asset.lower()}%{interval.lower()}%"),
                ).fetchall()
            }
            if wallet_markets:
                feed_snapshots = [s for s in feed_snapshots if s.market_slug in wallet_markets]
                if feed_snapshots:
                    market = feed_snapshots[0].market_slug
        all_snapshots.extend(feed_snapshots)
        feed_has_rows = bool(feed_snapshots)
        if not feed_snapshots:
            missing_sources.append(f"polymarket_1s:{polymarket_feed_db}")
    if venue in {"sqlite", "both", "local"} or (venue == "polymarket" and not all_snapshots):
        local_snapshots = load_snapshots(conn, market_slug=market, asset=asset, interval=interval, since=since)
        all_snapshots.extend(local_snapshots)
        if not local_snapshots:
            missing_sources.append("local_market_snapshots")
    snapshots = all_snapshots
    effective_market = market
    if not effective_market and snapshots:
        keys = sorted({s.market_slug or s.market_id or "unknown" for s in snapshots})
        if len(keys) == 1:
            effective_market = keys[0]
    if not snapshots:
        insert_missing_snapshot_notice(conn, market_slug=effective_market, dry_run=cfg.dry_run)
        note = "\nSnapshot source warning: no orderbook snapshots found. Checked " + ", ".join(missing_sources or ["configured sources"]) + "."
        return format_passive_pair_builder_report(conn, market_slug=effective_market, wallet=wallet) + note
    simulate_resting_quote_snapshots(conn, snapshots, cfg)
    report = format_passive_pair_builder_report(conn, market_slug=effective_market, wallet=wallet)
    if venue == "polymarket" and not feed_has_rows and polymarket_feed_db:
        report += f"\nSnapshot source warning: Polymarket 1s feed had no rows for this filter, fell back to local market_snapshots. feed_db={polymarket_feed_db}"
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Passive volatility-harvesting pair-builder simulator (read-only/paper-only).")
    parser.add_argument("--config", default="config.example.yaml")
    parser.add_argument("--market")
    parser.add_argument("--wallet")
    parser.add_argument("--asset", default="btc")
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--since", default="24h")
    parser.add_argument("--venue", choices=["polymarket", "both", "sqlite", "local"], default="polymarket")
    parser.add_argument("--polymarket-feed-db", default=None)
    parser.add_argument("--target-pair-cost", type=float, default=0.95)
    parser.add_argument("--hard-pair-cost-ceiling", type=float, default=0.99)
    parser.add_argument("--max-unpaired-qty", type=float, default=100.0)
    parser.add_argument("--edge-cents", type=float, default=3.0)
    parser.add_argument("--open-window-seconds", type=float, default=60.0)
    parser.add_argument("--repair-window-seconds", type=float, default=60.0)
    parser.add_argument("--max-pair-notional", type=float, default=2000.0)
    parser.add_argument("--max-side-notional", type=float, default=1000.0)
    parser.add_argument("--max-contract-notional", type=float, default=2000.0)
    parser.add_argument("--max-imbalance-ratio", type=float, default=2.0)
    parser.add_argument("--min-visible-liquidity", type=float, default=0.0)
    parser.add_argument("--max-spread", type=float, default=0.10)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--quote-stale-seconds", type=float, default=5.0)
    parser.add_argument("--quote-size", "--order-size", dest="order_size", type=float, default=10.0)
    parser.add_argument("--paper-only", action="store_true", help="Required safety label; simulator never submits orders.")
    parser.add_argument("--dry-run", action="store_true", help="Do not persist decisions.")
    args = parser.parse_args()
    cfg_file = load_config(args.config)
    default_feed = cfg_file.data.get("polymarket_1s_feed", {}).get("sqlite_path") or cfg_file.data.get("btc_feed", {}).get("polymarket_sqlite_path") or "/home/matt/workspace/kalshi-btc-15m-bot/feed/polymarket-btc-1s.sqlite3"
    polymarket_feed_db = args.polymarket_feed_db or default_feed
    conn = connect(cfg_file.db_path)
    initialize_schema(conn)
    sim_cfg = PassivePairBuilderConfig(
        target_pair_cost=args.target_pair_cost,
        hard_pair_cost_ceiling=args.hard_pair_cost_ceiling,
        max_unpaired_qty=args.max_unpaired_qty,
        edge_cents=args.edge_cents,
        open_window_seconds=args.open_window_seconds,
        repair_window_seconds=args.repair_window_seconds,
        max_pair_notional=args.max_pair_notional,
        max_side_notional=args.max_side_notional,
        max_contract_notional=args.max_contract_notional,
        max_imbalance_ratio=args.max_imbalance_ratio,
        min_visible_liquidity=args.min_visible_liquidity,
        max_spread=args.max_spread,
        quote_stale_seconds=args.quote_stale_seconds,
        order_size=args.order_size,
        paper_only=True,
        dry_run=args.dry_run,
    )
    if args.poll_seconds > 0 and False:  # keeps CLI option explicit without creating a daemon by default
        time.sleep(args.poll_seconds)
    print(run_from_db(conn, sim_cfg, market=args.market, wallet=args.wallet, asset=args.asset, interval=args.interval, since=args.since, venue=args.venue, polymarket_feed_db=polymarket_feed_db))


if __name__ == "__main__":
    main()
