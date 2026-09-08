"""
Macro agent — reads index-level data plus any macro-tagged news
(economy_macro, financial_markets, economy_monetary topics) as a proxy
for the broader market backdrop. A dedicated economic-calendar
connector (rate decisions, CPI, etc.) isn't wired up yet — add one to
ingestion/ and read from it here once it exists.
"""

from datetime import datetime, timedelta
from typing import List, Optional

from agents.base import call_claude, persist_signal
from store.db import get_session
from store.models import IndexSnapshot, NewsSentiment

MACRO_TOPICS = {"economy_macro", "financial_markets", "economy_monetary"}

SYSTEM_PROMPT = """
You are a macro/market-backdrop agent. You're given recent major index
levels and any macro-tagged news from the last day. Judge whether the
broader market backdrop currently favors higher or lower prices for
individual stocks.
"""


def analyze(symbol: str, index_symbols: Optional[List[str]] = None) -> None:
    index_symbols = index_symbols or ["SPX", "NDX"]
    since = datetime.utcnow() - timedelta(days=1)

    with get_session() as session:
        indices = [
            session.query(IndexSnapshot)
            .filter_by(index_symbol=idx)
            .order_by(IndexSnapshot.fetched_at.desc())
            .first()
            for idx in index_symbols
        ]
        indices = [i for i in indices if i is not None]

        macro_news = (
            session.query(NewsSentiment)
            .filter(NewsSentiment.symbol == symbol, NewsSentiment.published_at >= since)
            .all()
        )
        macro_news = [n for n in macro_news if n.topics and MACRO_TOPICS.intersection(n.topics)]

    if not indices and not macro_news:
        persist_signal(symbol, "macro", None, available=False)
        return

    summary = (
        f"Symbol: {symbol}\n"
        f"Index snapshots: {[(i.index_symbol, i.ohlcv) for i in indices]}\n"
        f"Macro-tagged news: {[n.headline for n in macro_news]}"
    )

    try:
        result = call_claude(SYSTEM_PROMPT, summary)
        persist_signal(symbol, "macro", result)
    except Exception:
        persist_signal(symbol, "macro", None, available=False)
