"""Tier 4 technical, core OHLCV, index, and options ingestion."""

from datetime import datetime, timedelta
from typing import Iterable, Optional

from ingestion.alpha_vantage_client import call
from ingestion.technical_rubric import evaluate
from store.db import get_session
from store.models import (
    CongressTrade,
    IndexSnapshot,
    InsiderTransaction,
    InstitutionalHolding,
    NewsSentiment,
    OptionSnapshot,
    TechnicalSnapshot,
)

CATALYST_TOPICS = {"mergers_and_acquisitions", "earnings", "financial_markets", "economy_fiscal", "economy_monetary", "economy_macro"}
CATALYST_TERMS = ("earnings", "guidance", "acquisition", "merger", "litigation", "lawsuit", "downgrade", "upgrade")

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
            options = call("REALTIME_OPTIONS", symbol=symbol)
            indicators["technical_rubric"] = evaluate(ohlcv, indicators, options)
            session.add(TechnicalSnapshot(symbol=symbol, timeframe=interval, ohlcv=ohlcv, indicators=indicators))
            session.add(OptionSnapshot(symbol=symbol, chain=options))
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


def catalyst_symbols(since: Optional[datetime] = None) -> set[str]:
    """Find watchlist symbols with new Tier 2/3 catalyst evidence."""
    since = since or datetime.utcnow() - timedelta(minutes=20)
    with get_session() as session:
        news_rows = (
            session.query(NewsSentiment)
            .filter(NewsSentiment.fetched_at >= since)
            .all()
        )
        reference_rows = (
            session.query(InsiderTransaction)
            .filter(InsiderTransaction.fetched_at >= since)
            .all()
        )
        reference_rows += session.query(CongressTrade).filter(CongressTrade.fetched_at >= since).all()
        reference_rows += session.query(InstitutionalHolding).filter(InstitutionalHolding.fetched_at >= since).all()

    symbols = {
        row.symbol
        for row in news_rows
        if (set(row.topics or []) & CATALYST_TOPICS)
        or any(term in (row.headline or "").lower() for term in CATALYST_TERMS)
        or abs(row.sentiment_score or 0) >= 0.35
    }
    symbols.update(row.symbol for row in reference_rows)
    return symbols


def refresh_on_catalyst(symbols: Iterable[str], since: Optional[datetime] = None) -> int:
    """Refresh only symbols with a new Tier 2/3 catalyst."""
    candidate_symbols = set(symbols) & catalyst_symbols(since)
    if not candidate_symbols:
        return 0
    run_technical(candidate_symbols)
    return len(candidate_symbols)
