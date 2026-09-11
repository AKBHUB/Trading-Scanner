"""
Runtime config store, backed by the app_config table, so parameters
(watchlist, tier cadences) can be changed from the frontend without a
redeploy. Falls back to config/schedule.yaml (plus a couple of hardcoded
defaults) the first time a key is read and nothing's been saved yet.
"""

from typing import Any

import yaml

from store.db import get_session
from store.models import AppConfig

with open("config/schedule.yaml") as f:
    _DEFAULTS = yaml.safe_load(f)

_DEFAULTS.setdefault("watchlist", ["AAPL", "MSFT", "NVDA"])
_DEFAULTS.setdefault("index_watchlist", ["SPX", "NDX"])
# Static SPX/NDX constituent list for Tier 3's third fallback rung — not
# auto-fetched from any index provider; maintained by hand via the
# dashboard as membership changes. Empty until set.
_DEFAULTS.setdefault("index_universe", [])


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


def resolve_fundamentals_universe() -> list:
    """
    Tier 3 (fundamentals) candidate universe, in priority order:
      1. fundamentals_watchlist — explicit override for this tier only
      2. watchlist — the main shared watchlist
      3. index_universe — static SPX/NDX constituent list, used only when
         neither of the above is set
    Centralized here because ingestion/scheduler.py and frontend/app.py
    both need this exact cascade and previously duplicated it inline.
    """
    return (
        get_config("fundamentals_watchlist", [])
        or get_config("watchlist", [])
        or get_config("index_universe", [])
    )
