"""
Runtime config store, backed by the app_config table, so parameters
(watchlist, tier cadences) can be changed from the frontend without a
redeploy. Falls back to config/schedule.yaml (plus a couple of hardcoded
defaults) the first time a key is read and nothing's been saved yet.
"""

from pathlib import Path
from typing import Any

import yaml

from store.db import get_session
from store.models import AppConfig

_SCHEDULE_PATH = Path(__file__).resolve().parent.parent / "config" / "schedule.yaml"

with _SCHEDULE_PATH.open() as f:
    _DEFAULTS = yaml.safe_load(f)

_DEFAULTS.setdefault("watchlist", ["AAPL", "MSFT", "NVDA"])
_DEFAULTS.setdefault("index_watchlist", ["SPX", "NDX"])
_DEFAULTS.setdefault("transaction_filters", {
    "min_insider_transaction_value": 100_000,
    "min_congress_transaction_value": 15_000,
    "min_institutional_holding_value": 1_000_000,
})


def get_config(key: str, default: Any = None) -> Any:
    with get_session() as session:
        row = session.get(AppConfig, key)
        if row is not None:
            return row.value
    return _DEFAULTS.get(key, default)


def set_config(key: str, value: Any) -> None:
    with get_session() as session:
        row = session.get(AppConfig, key)
        if row is None:
            session.add(AppConfig(key=key, value=value))
        else:
            row.value = value
