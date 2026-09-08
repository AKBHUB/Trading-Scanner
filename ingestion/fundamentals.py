"""
Tier 3 — event-triggered fundamentals.

Two triggers mark a symbol "dirty" so far (both Alpha Vantage-only):
its next earnings date has passed (EARNINGS_CALENDAR), or recent news
for that symbol carries the mergers_and_acquisitions topic — reusing
whatever Tier 1 already fetched, no extra API call needed for that
check. Either one queues a fresh fundamentals pull on the next run.

Not wired up yet: the new_8k_filing trigger from config/schedule.yaml.
That one comes from TradingView's filing tools, not Alpha Vantage —
add it as a third check function here once that connector exists.
"""

from datetime import datetime
from typing import Iterable

from ingestion.alpha_vantage_client import call, call_csv
from store.db import get_session
from store.models import CorporateActionFlag, Fundamentals, NewsSentiment

MA_TOPIC = "mergers_and_acquisitions"


def check_earnings_calendar_trigger(symbols: Iterable[str]) -> None:
    """Flag any symbol whose stored fundamentals have passed their valid_until."""
    rows = call_csv("EARNINGS_CALENDAR", horizon="3month")
    upcoming = {row["symbol"]: row.get("reportDate") for row in rows if row.get("symbol")}

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
                session.add(
                    CorporateActionFlag(
                        symbol=symbol,
                        reason="earnings_calendar_passed",
                        source_ref=upcoming.get(symbol),
                    )
                )


def check_news_ma_trigger(symbols: Iterable[str], since: datetime) -> None:
    """Flag any symbol whose recent Tier-1 news carries the M&A topic tag."""
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


def refresh_flagged_symbols() -> int:
    """Pull fresh fundamentals for every symbol with an unresolved flag."""
    refreshed = 0
    with get_session() as session:
        flags = session.query(CorporateActionFlag).filter_by(resolved=False).all()
        symbols = {f.symbol for f in flags}

        for symbol in symbols:
            earnings = call("EARNINGS", symbol=symbol)
            income = call("INCOME_STATEMENT", symbol=symbol)
            balance = call("BALANCE_SHEET", symbol=symbol)
            cash_flow = call("CASH_FLOW", symbol=symbol)

            reasons = [f.reason for f in flags if f.symbol == symbol]

            session.add(
                Fundamentals(
                    symbol=symbol,
                    fiscal_date_ending=_latest_fiscal_date(earnings),
                    valid_until=_next_report_date(earnings),
                    is_dirty=False,
                    dirty_reason=",".join(reasons),
                    income_statement=income,
                    balance_sheet=balance,
                    cash_flow=cash_flow,
                    earnings=earnings,
                )
            )
            refreshed += 1

        for flag in flags:
            flag.resolved = True

    return refreshed


def _next_report_date(earnings: dict):
    try:
        date_str = earnings["quarterlyEarnings"][0]["reportedDate"]
        return datetime.strptime(date_str, "%Y-%m-%d")
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _latest_fiscal_date(earnings: dict):
    try:
        date_str = earnings["quarterlyEarnings"][0]["fiscalDateEnding"]
        return datetime.strptime(date_str, "%Y-%m-%d")
    except (KeyError, IndexError, TypeError, ValueError):
        return None
