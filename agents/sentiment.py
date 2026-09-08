"""
Sentiment agent — reads today's news/sentiment timeline plus recent
insider transactions, congressional trades, and institutional holding
changes for a symbol. All four come from Tier 1 and Tier 2 ingestion;
this agent makes no API calls of its own.
"""

from datetime import datetime, timedelta

from agents.base import call_claude, persist_signal
from store.db import get_session
from store.models import CongressTrade, InsiderTransaction, InstitutionalHolding, NewsSentiment

SYSTEM_PROMPT = """
You are a sentiment and positioning agent. You're given today's news
sentiment for a symbol, plus recent insider transactions, congressional
trades, and institutional holding changes. Judge whether the combined
sentiment/positioning picture favors higher or lower prices.
"""


def analyze(symbol: str) -> None:
    since = datetime.utcnow() - timedelta(days=1)

    with get_session() as session:
        news = (
            session.query(NewsSentiment)
            .filter(NewsSentiment.symbol == symbol, NewsSentiment.published_at >= since)
            .all()
        )
        insider = (
            session.query(InsiderTransaction)
            .filter_by(symbol=symbol)
            .order_by(InsiderTransaction.transaction_date.desc())
            .limit(10)
            .all()
        )
        congress = (
            session.query(CongressTrade)
            .filter_by(symbol=symbol)
            .order_by(CongressTrade.transaction_date.desc())
            .limit(10)
            .all()
        )
        institutional = (
            session.query(InstitutionalHolding)
            .filter_by(symbol=symbol)
            .order_by(InstitutionalHolding.report_date.desc())
            .limit(10)
            .all()
        )

    if not news and not insider and not congress and not institutional:
        persist_signal(symbol, "sentiment", None, available=False)
        return

    summary = (
        f"Symbol: {symbol}\n"
        f"Today's news sentiment scores: {[n.sentiment_score for n in news]}\n"
        f"Headlines: {[n.headline for n in news]}\n"
        f"Recent insider transactions: {[(i.transaction_type, i.shares) for i in insider]}\n"
        f"Recent congress trades: {[(c.transaction_type, c.amount_range) for c in congress]}\n"
        f"Recent institutional holding changes (%): {[i.change_pct for i in institutional]}"
    )

    try:
        result = call_claude(SYSTEM_PROMPT, summary)
        persist_signal(symbol, "sentiment", result)
    except Exception:
        persist_signal(symbol, "sentiment", None, available=False)
