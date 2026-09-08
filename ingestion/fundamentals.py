"""
Tier 3 — event-triggered fundamentals, backed by yfinance instead of
Alpha Vantage. Nothing in this module calls Alpha Vantage anymore —
EARNINGS_CALENDAR, EARNINGS, INCOME_STATEMENT, BALANCE_SHEET, and
CASH_FLOW have all been replaced by `yfinance.Ticker`.

Three ways a symbol's fundamentals get (re)fetched:

  1. Event-triggered: its next earnings date has passed (yfinance's own
     `Ticker.calendar` / `Ticker.get_earnings_dates()`), or recent Tier-1
     news carries the M&A topic tag (reusing whatever Tier 1 already
     fetched — no extra call). Either flips a CorporateActionFlag,
     picked up by refresh_flagged_symbols() on its next scheduled pass.
  2. Weekly safety-net refresh (config/schedule.yaml:
     tiers.fundamentals.weekly_refresh) — force-refreshes every
     candidate via refresh_all() regardless of dirty state, catching
     anything the event triggers missed (e.g. a ticker where yfinance's
     earnings-date field was temporarily missing).
  3. Lazy, on-demand: agents/fundamental.py calls fetch_and_store_metrics()
     directly the first time it's asked to analyze a symbol with no
     Fundamentals row at all, instead of just reporting unavailable.

Not wired up yet: the new_8k_filing trigger from config/schedule.yaml.
That one comes from TradingView's filing tools, not yfinance or Alpha
Vantage — add it as a third check_* function here once that connector
exists.
"""

import logging
from datetime import datetime
from typing import Any, Dict, Iterable

import yfinance as yf

from store.db import get_session
from store.models import CorporateActionFlag, Fundamentals, NewsSentiment

logger = logging.getLogger(__name__)

MA_TOPIC = "mergers_and_acquisitions"

# yfinance Ticker.info key -> flattened column label, matching the metrics
# actually needed for fundamental analysis (not full statements).
METRIC_FIELDS = {
    "Market Capitalization": "marketCap",
    "Trailing P/E": "trailingPE",
    "Forward P/E": "forwardPE",
    "Price-to-Book (P/B)": "priceToBook",
    "Beta (5Y Monthly)": "beta",
    "Dividend Yield": "dividendYield",
    "Revenue Growth (YoY)": "revenueGrowth",
    "Quarterly Earnings Growth (YoY)": "earningsGrowth",
    "Profit Margin": "profitMargins",
    "Operating Margin": "operatingMargins",
}


def fetch_fundamental_metrics(symbol: str) -> Dict[str, Any]:
    """One yfinance call (`Ticker.info`) per symbol. Unlike Alpha Vantage's
    EARNINGS_CALENDAR, yfinance has no multi-ticker batch endpoint, so this
    can't be combined across symbols the way Tier 1's NEWS_SENTIMENT call is."""
    ticker = yf.Ticker(symbol)
    info = ticker.info or {}
    metrics = {"Ticker": symbol}
    for label, key in METRIC_FIELDS.items():
        metrics[label] = info.get(key)
    return metrics


def _next_earnings_date(ticker: "yf.Ticker"):
    """
    yfinance has no single stable field for this across versions. Try
    `.calendar` first — a dict with an 'Earnings Date' list of `date`
    objects in current yfinance — then fall back to
    `.get_earnings_dates()`, a DataFrame mixing past and future rows
    indexed by tz-aware timestamp, from which we take the earliest
    entry that's still in the future. Returns None (not an error) if
    neither has anything usable — a real possibility for smaller/newer
    tickers yfinance doesn't have earnings-calendar data for yet.
    """
    try:
        calendar = ticker.calendar
        if isinstance(calendar, dict):
            dates = calendar.get("Earnings Date")
            if dates:
                d = dates[0]
                return datetime(d.year, d.month, d.day)
    except Exception as e:
        logger.debug("Ticker.calendar failed: %s: %s", type(e).__name__, e)

    try:
        earnings_dates = ticker.get_earnings_dates(limit=8)
        if earnings_dates is not None and not earnings_dates.empty:
            now = datetime.utcnow()
            upcoming = [
                idx.to_pydatetime().replace(tzinfo=None)
                for idx in earnings_dates.index
                if idx.to_pydatetime().replace(tzinfo=None) >= now
            ]
            if upcoming:
                return min(upcoming)
    except Exception as e:
        logger.debug("get_earnings_dates() failed: %s: %s", type(e).__name__, e)

    return None


def check_earnings_calendar_trigger(symbols: Iterable[str]) -> None:
    """Flag any symbol whose stored fundamentals have passed their
    valid_until (next expected earnings date)."""
    with get_session() as session:
        for symbol in symbols:
            latest = (
                session.query(Fundamentals)
                .filter(Fundamentals.symbol == symbol)
                .order_by(Fundamentals.fetched_at.desc())
                .first()
            )
            is_stale = latest is None or (latest.valid_until and latest.valid_until <= datetime.utcnow())
            if is_stale:
                session.add(CorporateActionFlag(symbol=symbol, reason="earnings_calendar_passed"))


def check_news_ma_trigger(symbols: Iterable[str], since: datetime) -> None:
    """Flag any symbol whose recent Tier-1 news carries the M&A topic tag.
    Reads the store, not an external API — unaffected by the Alpha
    Vantage -> yfinance switch."""
    symbols = list(symbols)
    with get_session() as session:
        rows = (
            session.query(NewsSentiment)
            .filter(NewsSentiment.symbol.in_(symbols))
            .filter(NewsSentiment.published_at >= since)
            .all()
        )
        for row in rows:
            if row.topics and MA_TOPIC in row.topics:
                session.add(
                    CorporateActionFlag(
                        symbol=row.symbol,
                        reason="news_topic",
                        source_ref=f"{MA_TOPIC}:{row.id}",
                    )
                )


def fetch_and_store_metrics(symbol: str, dirty_reason: str = "") -> Fundamentals:
    """
    Fetch + persist one symbol's fundamentals right now, unconditionally.
    Used by refresh_flagged_symbols(), refresh_all() (the weekly job and
    the dashboard's quick-refresh button), and the fundamental agent's
    lazy on-demand fetch when it finds no cached row at all.
    """
    ticker = yf.Ticker(symbol)
    info = ticker.info or {}
    metrics = {"Ticker": symbol}
    for label, key in METRIC_FIELDS.items():
        metrics[label] = info.get(key)

    final = Fundamentals(
        symbol=symbol,
        valid_until=_next_earnings_date(ticker),
        is_dirty=False,
        dirty_reason=dirty_reason,
        metrics=metrics,
    )
    with get_session() as session:
        session.add(final)
    return final


def refresh_flagged_symbols() -> int:
    """
    Pull fresh fundamentals for every symbol with an unresolved flag. Each
    symbol is fetched and resolved independently — one bad ticker (yfinance
    is an unofficial, sometimes-flaky API) logs and moves on rather than
    aborting the rest of the batch, unlike the old Alpha Vantage version
    where one failing call rolled back everything in the same transaction.
    """
    with get_session() as session:
        flags = session.query(CorporateActionFlag).filter_by(resolved=False).all()

    reasons_by_symbol: Dict[str, list] = {}
    for f in flags:
        reasons_by_symbol.setdefault(f.symbol, []).append(f.reason)

    refreshed = 0
    resolved_symbols = []
    for symbol, reasons in reasons_by_symbol.items():
        try:
            fetch_and_store_metrics(symbol, dirty_reason=",".join(reasons))
            refreshed += 1
            resolved_symbols.append(symbol)
        except Exception as e:
            logger.warning("fundamentals refresh failed for %s: %s: %s", symbol, type(e).__name__, e)

    if resolved_symbols:
        with get_session() as session:
            session.query(CorporateActionFlag).filter(
                CorporateActionFlag.symbol.in_(resolved_symbols),
                CorporateActionFlag.resolved == False,  # noqa: E712
            ).update({"resolved": True}, synchronize_session=False)

    return refreshed


def refresh_all(symbols: Iterable[str]) -> int:
    """Force-refresh every candidate regardless of dirty state — the
    weekly safety-net job and the dashboard's quick-refresh button. Same
    per-symbol isolation as refresh_flagged_symbols()."""
    count = 0
    for symbol in symbols:
        try:
            fetch_and_store_metrics(symbol, dirty_reason="weekly_refresh")
            count += 1
        except Exception as e:
            logger.warning("fundamentals refresh failed for %s: %s: %s", symbol, type(e).__name__, e)
    return count
