"""
Tier 4 — technical indicators, core OHLCV, index data, and options,
all fetched on the same interval (config/schedule.yaml: tiers.technical_core).
"""

from typing import Iterable

from ingestion.alpha_vantage_client import call
from store.db import get_session
from store.models import IndexSnapshot, OptionSnapshot, TechnicalSnapshot

# Matches the indicators already used by swing-trade-scanner's 17-point rubric.
INDICATOR_FUNCTIONS = ["RSI", "MACD", "EMA", "SMA", "BBANDS", "OBV", "ADX"]

# Alpha Vantage requires different parameter sets per indicator function —
# time_period and series_type aren't universal, so each is only sent to the
# functions that actually accept it (sending it to ones that don't causes
# "Invalid API call" errors, as BBANDS did here).
_NEEDS_TIME_PERIOD = {"RSI", "EMA", "SMA", "BBANDS", "ADX"}
_NEEDS_SERIES_TYPE = {"RSI", "EMA", "SMA", "BBANDS", "MACD"}


def fetch_ohlcv(symbol: str, interval: str = "15min") -> dict:
    return call("TIME_SERIES_INTRADAY", symbol=symbol, interval=interval, outputsize="compact")


def fetch_indicators(symbol: str, interval: str = "15min") -> dict:
    indicators = {}
    for function in INDICATOR_FUNCTIONS:
        params = {"symbol": symbol, "interval": interval}
        if function in _NEEDS_TIME_PERIOD:
            params["time_period"] = 14
        if function in _NEEDS_SERIES_TYPE:
            params["series_type"] = "close"
        indicators[function] = call(function, **params)
    return indicators


def run_technical(symbols: Iterable[str], interval: str = "15min") -> int:
    written = 0
    with get_session() as session:
        for symbol in symbols:
            ohlcv = fetch_ohlcv(symbol, interval)
            indicators = fetch_indicators(symbol, interval)
            session.add(TechnicalSnapshot(symbol=symbol, timeframe=interval, ohlcv=ohlcv, indicators=indicators))
            written += 1
    return written


def run_index_data(index_symbols: Iterable[str]) -> int:
    written = 0
    with get_session() as session:
        for index_symbol in index_symbols:
            data = call("INDEX_DATA", symbol=index_symbol)
            session.add(IndexSnapshot(index_symbol=index_symbol, ohlcv=data))
            written += 1
    return written


def run_options(symbols: Iterable[str]) -> int:
    written = 0
    with get_session() as session:
        for symbol in symbols:
            data = call("REALTIME_OPTIONS", symbol=symbol)
            session.add(OptionSnapshot(symbol=symbol, chain=data))
            written += 1
    return written
