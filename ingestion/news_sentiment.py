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


def within_window(start_time: str, end_time: str, now: Optional[datetime] = None, tz: str = "America/Chicago") -> bool:
    """start_time/end_time are 'HH:MM' strings, evaluated in `tz` (defaults to
    the config timezone) rather than the server's local time — on Railway
    the container clock is UTC, so comparing against a naive datetime.now()
    made this window wrong by several hours."""
    now = now or datetime.now(ZoneInfo(tz))
    start = dt_time.fromisoformat(start_time)
    end = dt_time.fromisoformat(end_time)
    return start <= now.time() <= end


def fetch_news_sentiment(symbols: Iterable[str], limit: int = 50) -> list:
    """One Alpha Vantage call covers every symbol via a comma-separated tickers param."""
    tickers = ",".join(symbols)
    data = call("NEWS_SENTIMENT", tickers=tickers, limit=limit)
    return data.get("feed", [])


def run(symbols: Iterable[str]) -> int:
    """Fetch and persist news/sentiment for `symbols`. Returns rows written."""
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
                if symbol not in symbols:
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
