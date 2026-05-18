from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            dt = datetime.fromtimestamp(float(text), tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_ts(value: Any) -> str | None:
    dt = parse_ts(value)
    return dt.isoformat() if dt else None


def seconds_between(start: Any, end: Any) -> int | None:
    a = parse_ts(start)
    b = parse_ts(end)
    if not a or not b:
        return None
    return int((b - a).total_seconds())


def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def dump_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def write_raw_json(raw_dir: str | Path, source: str, payload: Any) -> Path:
    root = Path(raw_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{source}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return path
