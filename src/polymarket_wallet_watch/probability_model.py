from __future__ import annotations

import argparse
import math
import sqlite3
from dataclasses import dataclass
from statistics import median
from typing import Any

from .adapter_polymarket_1s import DEFAULT_POLYMARKET_1S_DB, connect_feed, load_snapshots


@dataclass
class ProbabilityModelConfig:
    vol_floor_per_sqrt_second: float = 1.0
    edge_threshold: float = 0.03
    min_seconds_to_close: float = 1.0


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _realized_vol(values: list[float], seconds: float) -> float | None:
    if len(values) < 2 or seconds <= 0:
        return None
    diffs = [values[i] - values[i - 1] for i in range(1, len(values))]
    if not diffs:
        return None
    mean = sum(diffs) / len(diffs)
    variance = sum((d - mean) ** 2 for d in diffs) / max(len(diffs) - 1, 1)
    return math.sqrt(max(variance, 0.0))


def _trend_slope(values: list[float], seconds: float) -> float | None:
    if len(values) < 2 or seconds <= 0:
        return None
    return (values[-1] - values[0]) / seconds


def model_yes_probability(distance_from_strike: float, seconds_to_close: float, sigma_per_sqrt_second: float, *, trend_adjustment: float = 0.0, cfg: ProbabilityModelConfig | None = None) -> tuple[float, float]:
    cfg = cfg or ProbabilityModelConfig()
    t = max(float(seconds_to_close), cfg.min_seconds_to_close)
    sigma = max(abs(float(sigma_per_sqrt_second)), cfg.vol_floor_per_sqrt_second)
    expected_move = sigma * math.sqrt(t)
    adjusted_distance = float(distance_from_strike) + float(trend_adjustment) * t
    z = adjusted_distance / expected_move if expected_move > 0 else 0.0
    return round(_normal_cdf(z), 6), round(z, 6)


def enrich_probability_features(snapshots: list[dict[str, Any]], *, cfg: ProbabilityModelConfig | None = None) -> list[dict[str, Any]]:
    cfg = cfg or ProbabilityModelConfig()
    rows = sorted(snapshots, key=lambda r: r.get("ts") or "")
    out: list[dict[str, Any]] = []
    prices: list[float] = []
    for snap in rows:
        btc = _safe_float(snap.get("btc_price"))
        strike = _safe_float(snap.get("strike"))
        seconds_to_close = _safe_float(snap.get("seconds_to_close"))
        yes_mid = None
        yes_bid = _safe_float(snap.get("yes_bid"))
        yes_ask = _safe_float(snap.get("yes_ask"))
        no_mid = None
        no_bid = _safe_float(snap.get("no_bid"))
        no_ask = _safe_float(snap.get("no_ask"))
        if yes_bid is not None and yes_ask is not None:
            yes_mid = (yes_bid + yes_ask) / 2
        if no_bid is not None and no_ask is not None:
            no_mid = (no_bid + no_ask) / 2
        if btc is not None:
            prices.append(btc)
        window_30 = prices[-31:]
        window_60 = prices[-61:]
        window_180 = prices[-181:]
        vol30 = _realized_vol(window_30, max(len(window_30) - 1, 1))
        vol60 = _realized_vol(window_60, max(len(window_60) - 1, 1))
        vol180 = _realized_vol(window_180, max(len(window_180) - 1, 1))
        trend = _trend_slope(window_30, max(len(window_30) - 1, 1))
        sigma = vol60 or vol30 or vol180 or cfg.vol_floor_per_sqrt_second
        distance = btc - strike if btc is not None and strike is not None else None
        prob = z = yes_edge = no_edge = None
        if distance is not None and seconds_to_close is not None:
            prob, z = model_yes_probability(distance, seconds_to_close, sigma, trend_adjustment=trend or 0.0, cfg=cfg)
            if yes_mid is not None:
                yes_edge = round(prob - yes_mid, 6)
            if no_mid is not None:
                no_edge = round((1.0 - prob) - no_mid, 6)
        out.append({
            "ts": snap.get("ts"),
            "market_key": snap.get("market_key") or snap.get("market_slug"),
            "market_slug": snap.get("market_slug"),
            "btc_price": btc,
            "strike": strike,
            "distance_from_strike": round(distance, 6) if distance is not None else None,
            "seconds_to_close": seconds_to_close,
            "realized_vol_30s": round(vol30, 6) if vol30 is not None else None,
            "realized_vol_60s": round(vol60, 6) if vol60 is not None else None,
            "realized_vol_180s": round(vol180, 6) if vol180 is not None else None,
            "trend_slope_30s": round(trend, 6) if trend is not None else None,
            "z_score": z,
            "model_yes_probability": prob,
            "model_no_probability": round(1.0 - prob, 6) if prob is not None else None,
            "market_yes_mid": round(yes_mid, 6) if yes_mid is not None else None,
            "market_no_mid": round(no_mid, 6) if no_mid is not None else None,
            "yes_edge": yes_edge,
            "no_edge": no_edge,
            "yes_quote_signal": bool(yes_edge is not None and yes_edge >= cfg.edge_threshold),
            "no_quote_signal": bool(no_edge is not None and no_edge >= cfg.edge_threshold),
        })
    return out


def summarize_probability_features(rows: list[dict[str, Any]], *, cfg: ProbabilityModelConfig | None = None) -> dict[str, Any]:
    cfg = cfg or ProbabilityModelConfig()
    usable = [r for r in rows if r.get("model_yes_probability") is not None]
    yes_edges = [float(r["yes_edge"]) for r in usable if r.get("yes_edge") is not None]
    no_edges = [float(r["no_edge"]) for r in usable if r.get("no_edge") is not None]
    latest = usable[-1] if usable else None
    def _med(vals: list[float]) -> float | None:
        return round(median(vals), 6) if vals else None
    return {
        "snapshot_count": len(rows),
        "usable_snapshot_count": len(usable),
        "latest": latest,
        "median_yes_edge": _med(yes_edges),
        "median_no_edge": _med(no_edges),
        "yes_signal_count": sum(1 for r in usable if r.get("yes_quote_signal")),
        "no_signal_count": sum(1 for r in usable if r.get("no_quote_signal")),
        "edge_threshold": cfg.edge_threshold,
        "safety": "READ_ONLY_RESEARCH: no private keys, no signers, no orders, no live trading.",
    }


def format_probability_report(summary: dict[str, Any], rows: list[dict[str, Any]], *, limit: int = 10) -> str:
    latest = summary.get("latest") or {}
    signal_rows = [r for r in rows if r.get("yes_quote_signal") or r.get("no_quote_signal")]
    lines = [
        "Polymarket Probability Model — READ-ONLY RESEARCH",
        "Safety: local feed analysis only; no private keys, no signers, no orders.",
        "Model: z = distance_from_strike / (realized_vol * sqrt(seconds_to_close)); probability = normal_cdf(z).",
        "Purpose: decide whether a cheap side is actually discounted vs distance/time/volatility, not to claim profitability.",
        f"Snapshots: {summary.get('usable_snapshot_count', 0)} usable / {summary.get('snapshot_count', 0)} total",
        f"Edge threshold: {summary.get('edge_threshold')}",
        f"Median YES edge: {summary.get('median_yes_edge')} | Median NO edge: {summary.get('median_no_edge')}",
        f"YES signal count: {summary.get('yes_signal_count')} | NO signal count: {summary.get('no_signal_count')}",
        "",
        "Latest snapshot:",
        f"  market={latest.get('market_slug') or latest.get('market_key')} ts={latest.get('ts')}",
        f"  btc={latest.get('btc_price')} strike={latest.get('strike')} distance={latest.get('distance_from_strike')} seconds_to_close={latest.get('seconds_to_close')}",
        f"  vol60={latest.get('realized_vol_60s')} slope30={latest.get('trend_slope_30s')} z={latest.get('z_score')}",
        f"  model_yes={latest.get('model_yes_probability')} market_yes_mid={latest.get('market_yes_mid')} yes_edge={latest.get('yes_edge')}",
        f"  model_no={latest.get('model_no_probability')} market_no_mid={latest.get('market_no_mid')} no_edge={latest.get('no_edge')}",
        "",
        "Recent quote signals:",
    ]
    for r in signal_rows[-limit:]:
        side = "YES" if r.get("yes_quote_signal") else "NO"
        edge = r.get("yes_edge") if side == "YES" else r.get("no_edge")
        lines.append(f"  {r.get('ts')} {r.get('market_slug')} side={side} edge={edge} model_yes={r.get('model_yes_probability')} yes_mid={r.get('market_yes_mid')} z={r.get('z_score')} d={r.get('distance_from_strike')} t={r.get('seconds_to_close')}")
    if not signal_rows:
        lines.append("  No model-edge signals above threshold in this window.")
    return "\n".join(lines)


def run_probability_model(feed_db: str, *, market: str | None = None, since: str | None = "24h", edge_threshold: float = 0.03, limit: int | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    conn = connect_feed(feed_db)
    try:
        snapshots = load_snapshots(conn, market_key=market, since=since, limit=limit)
    finally:
        conn.close()
    cfg = ProbabilityModelConfig(edge_threshold=edge_threshold)
    rows = enrich_probability_features(snapshots, cfg=cfg)
    return summarize_probability_features(rows, cfg=cfg), rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only distance/time/volatility probability model for Polymarket BTC up/down snapshots.")
    parser.add_argument("--feed-db", default=str(DEFAULT_POLYMARKET_1S_DB))
    parser.add_argument("--market")
    parser.add_argument("--since", default="24h")
    parser.add_argument("--edge-threshold", type=float, default=0.03)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    summary, rows = run_probability_model(args.feed_db, market=args.market, since=args.since, edge_threshold=args.edge_threshold, limit=args.limit)
    print(format_probability_report(summary, rows))


if __name__ == "__main__":
    main()
