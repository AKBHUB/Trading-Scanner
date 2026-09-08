"""
Tier 1 — intraday news & sentiment.

Meant to run on an interval within a configured time window (see
config/schedule.yaml: tiers.news_sentiment). Appends rows to
news_sentiment rather than overwriting, so agents can read the day's
sentiment timeline, not just the latest snapshot.
"""

from datetime import datetime, time as dt_time
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from ingestion.alpha_vantage_client import call
from store.db import get_session
from store.models import NewsSentiment

# Every topic Alpha Vantage's NEWS_SENTIMENT supports — not used to filter
# the fallback call (see fetch_news_sentiment for why), but kept here as a
# reference for anything that wants to filter the *stored* topics column,
# e.g. fundamentals.check_news_ma_trigger already does this against
# NewsSentiment.topics after the fact.
ALL_NEWS_TOPICS = [
    "mergers_and_acquisitions",
    "financial_markets",
    "economy_fiscal",
    "economy_monetary",
    "economy_macro",
    "energy_transportation",
    "finance",
    "life_sciences",
    "manufacturing",
    "real_estate",
    "retail_wholesale",
    "technology",
]

DEFAULT_LIMIT = 50
# The market-wide fallback below has no tickers to narrow it, so pull more
# per call to get reasonable breadth out of the one request.
TOPIC_MODE_LIMIT = 200


def within_window(start_time: str, end_time: str, now: Optional[datetime] = None, tz: str = "America/Chicago") -> bool:
    """start_time/end_time are 'HH:MM' strings, evaluated in `tz` (defaults to
    the config timezone) rather than the server's local time — on Railway
    the container clock is UTC, so comparing against a naive datetime.now()
    made this window wrong by several hours."""
    now = now or datetime.now(ZoneInfo(tz))
    start = dt_time.fromisoformat(start_time)
    end = dt_time.fromisoformat(end_time)
    return start <= now.time() <= end


def fetch_news_sentiment(symbols: Iterable[str], limit: int = DEFAULT_LIMIT) -> list:
    """
    Ticker-scoped when `symbols` is non-empty (one call, comma-separated
    tickers param). With no symbols — no watchlist saved — falls back to
    Alpha Vantage's general top-financial-news feed instead of silently
    fetching nothing.

    NOT topics=<all 12 topics>: verified against the live API that
    Alpha Vantage ANDs multiple topics rather than ORing them, the same
    way multiple tickers OR — so a comma-separated list of unrelated
    topics (M&A + fiscal policy + energy/transportation, say) matches no
    article and always returns an empty feed. Omitting both tickers and
    topics gets the real general feed instead, at the same cost (one
    call). Each article still carries its own topics array, which is
    stored per row below, so anything filtering on NewsSentiment.topics
    downstream (e.g. fundamentals.check_news_ma_trigger) is unaffected.
    """
    symbols = list(symbols)
    if symbols:
        data = call("NEWS_SENTIMENT", tickers=",".join(symbols), limit=limit)
    else:
        data = call("NEWS_SENTIMENT", limit=max(limit, TOPIC_MODE_LIMIT))
    return data.get("feed", [])


def run(symbols: Iterable[str]) -> int:
    """
    Fetch and persist news/sentiment. Returns rows written.

    With a watchlist, writes one row per (article, symbol) restricted to
    those symbols. With no watchlist, pulls the general market feed instead
    and writes one row per (article, symbol) for every ticker Alpha Vantage
    tagged the article with — there's no watchlist left to filter against.
    """
    symbols = set(symbols)
    feed = fetch_news_sentiment(symbols)
    written = 0

    with get_session() as session:
        for item in feed:
            published_at = _parse_av_timestamp(item.get("time_published"))
            topics = [t["topic"] for t in item.get("topics", [])]

            # Alpha Vantage returns sentiment per ticker inside ticker_sentiment;
            # write one row per (article, symbol) so scores stay symbol-specific.
            for ticker_sentiment in item.get("ticker_sentiment", []):
                symbol = ticker_sentiment.get("ticker")
                if not symbol:
                    continue
                if symbols and symbol not in symbols:
                    continue
                session.add(
                    NewsSentiment(
                        symbol=symbol,
                        published_at=published_at,
                        headline=item.get("title"),
                        source=item.get("source"),
                        sentiment_score=_safe_float(ticker_sentiment.get("ticker_sentiment_score")),
                        relevance_score=_safe_float(ticker_sentiment.get("relevance_score")),
                        topics=topics,
                        raw=item,
                    )
                )
                written += 1

    return written


def _parse_av_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    # Alpha Vantage format: YYYYMMDDTHHMMSS
    return datetime.strptime(value, "%Y%m%dT%H%M%S")


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
