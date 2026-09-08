"""
Fundamental agent — reads the current (non-stale) Fundamentals row for
a symbol, written by ingestion/fundamentals.py. Since fundamentals only
refresh on earnings or a flagged corporate action, this agent is
reading a cached view most days rather than triggering any fetch.
"""

from agents.base import call_claude, persist_signal
from store.db import get_session
from store.models import Fundamentals

SYSTEM_PROMPT = """
You are a fundamental analysis agent. You're given a company's latest
income statement, balance sheet, cash flow, and earnings history.
Judge whether the fundamentals support higher or lower prices going
forward — growth trajectory, margins, balance sheet health, earnings
surprises.
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
        persist_signal(symbol, "fundamental", None, available=False)
        return

    summary = (
        f"Symbol: {symbol}\n"
        f"Fiscal date: {snapshot.fiscal_date_ending}\n"
        f"Earnings: {snapshot.earnings}\n"
        f"Income statement: {snapshot.income_statement}\n"
        f"Balance sheet: {snapshot.balance_sheet}\n"
        f"Cash flow: {snapshot.cash_flow}"
    )

    try:
        result = call_claude(SYSTEM_PROMPT, summary)
        persist_signal(symbol, "fundamental", result)
    except Exception as e:
        persist_signal(symbol, "fundamental", None, available=False, error=f"{type(e).__name__}: {e}")
