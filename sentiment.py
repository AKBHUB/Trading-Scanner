"""
Sentiment agent — reads today's news/sentiment timeline plus recent
insider transactions, congressional trades, and institutional holding
changes for a symbol. All four come from Tier 1 and Tier 2 ingestion;
this agent makes no API calls of its own.

Tier 2 large-volume filter: insider transactions and institutional
holdings are filtered to a flat share-count floor before they reach the
prompt — a simple absolute threshold rather than anything relative to
average daily volume or float. Congress trades are exempted: Alpha
Vantage's disclosures only give a dollar amount_range (e.g. "$1,001 -
$15,000"), never a share count, so there's nothing to threshold there.
For institutional holdings the threshold applies to shares_held (the
reported position size), not the period-over-period change — simplest
reading of "large number of shares," easy to switch to a change-based
filter later if position size turns out to be the wrong signal.
"""

from datetime import datetime, timedelta

from agents.base import call_claude, persist_signal
from store.config_store import get_config
from store.db import get_session
from store.models import CongressTrade, InsiderTransaction, InstitutionalHolding, NewsSentiment

SYSTEM_PROMPT = """
You are a sentiment and positioning agent. You're given today's news
sentiment for a symbol, plus recent insider transactions, congressional
trades, and institutional holding changes. Judge whether the combined
sentiment/positioning picture favors higher or lower prices.
"""

# Flat share-count floors, not relative to ADV/float. Tunable via config;
# these are just the defaults when nothing's been saved.
INSIDER_LARGE_SHARES_DEFAULT = 10_000
INSTITUTIONAL_LARGE_SHARES_DEFAULT = 100_000


def analyze(symbol: str) -> None:
    since = datetime.utcnow() - timedelta(days=1)
    insider_floor = get_config("insider_large_shares_threshold", INSIDER_LARGE_SHARES_DEFAULT)
    institutional_floor = get_config("institutional_large_shares_threshold", INSTITUTIONAL_LARGE_SHARES_DEFAULT)

    with get_session() as session:
        news = (
            session.query(NewsSentiment)
            .filter(NewsSentiment.symbol == symbol, NewsSentiment.published_at >= since)
            .all()
        )
        insider = (
            session.query(InsiderTransaction)
            .filter_by(symbol=symbol)
            .filter(InsiderTransaction.shares >= insider_floor)
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
            .filter(InstitutionalHolding.shares_held >= institutional_floor)
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
        f"Recent insider transactions (>= {insider_floor:,.0f} shares): "
        f"{[(i.transaction_type, i.shares) for i in insider]}\n"
        f"Recent congress trades: {[(c.transaction_type, c.amount_range) for c in congress]}\n"
        f"Recent institutional holding changes, positions >= {institutional_floor:,.0f} shares (%): "
        f"{[i.change_pct for i in institutional]}"
    )

    try:
        result = call_claude(SYSTEM_PROMPT, summary)
        persist_signal(symbol, "sentiment", result)
    except Exception as e:
        persist_signal(symbol, "sentiment", None, available=False, error=f"{type(e).__name__}: {e}")
