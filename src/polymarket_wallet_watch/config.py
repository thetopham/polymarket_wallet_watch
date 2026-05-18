from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class AppConfig:
    data: dict[str, Any]
    path: Path | None = None

    @property
    def db_path(self) -> Path:
        return Path(self.data.get("database", {}).get("path", "data/polymarket_wallet_watch.sqlite3"))


def load_config(path: str | Path = "config.example.yaml") -> AppConfig:
    cfg_path = Path(path)
    data = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
    return AppConfig(data=data or {}, path=cfg_path if cfg_path.exists() else None)
