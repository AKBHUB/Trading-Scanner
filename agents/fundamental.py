"""
Fundamental agent — reads the current (non-stale) Fundamentals row for a
symbol, written by ingestion/fundamentals.py (yfinance-backed: market cap,
P/E, P/B, beta, dividend yield, revenue/earnings growth, margins).

Since fundamentals only refresh on earnings, a flagged corporate action,
or the weekly safety-net job, this agent is usually reading a cached view
rather than triggering a fetch — except the very first time a symbol is
analyzed with no Fundamentals row at all, where it fetches on the spot
instead of just reporting unavailable.
"""

from ingestion.fundamentals import fetch_and_store_metrics
from agents.base import call_claude, persist_signal
from store.db import get_session
from store.models import Fundamentals

SYSTEM_PROMPT = """
You are a fundamental analysis agent. You're given a company's key
valuation and growth metrics — market cap, trailing/forward P/E,
price-to-book, beta, dividend yield, revenue growth, quarterly earnings
growth, profit margin, operating margin. Judge whether the fundamentals
support higher or lower prices going forward.
"""


def analyze(symbol: str) -> None:
    with get_session() as session:
        snapshot = (
            session.query(Fundamentals)
            .filter_by(symbol=symbol)
            .order_by(Fundamentals.fetched_at.desc())
            .first()
        )

    if snapshot is None:
        # No cached fundamentals at all for this symbol — fetch now rather
        # than reporting unavailable, so the first analysis of a new
        # symbol doesn't have to wait for the next scheduled/event-
        # triggered refresh.
        try:
            snapshot = fetch_and_store_metrics(symbol, dirty_reason="lazy_fetch_on_analyze")
        except Exception as e:
            persist_signal(symbol, "fundamental", None, available=False, error=f"{type(e).__name__}: {e}")
            return

    summary = f"Symbol: {symbol}\nFiscal date: {snapshot.fiscal_date_ending}\nMetrics: {snapshot.metrics}"

    try:
        result = call_claude(SYSTEM_PROMPT, summary)
        persist_signal(symbol, "fundamental", result)
    except Exception as e:
        persist_signal(symbol, "fundamental", None, available=False, error=f"{type(e).__name__}: {e}")
