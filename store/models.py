"""
Data store schema for Trading-Scanner.

One table per ingestion tier, each tagged with `fetched_at` (when this
row was pulled) so agents and skills can reason about freshness without
re-deriving it. Fundamentals additionally carry `valid_until` /
`is_dirty` so the event-triggered refresh logic has somewhere to live.

Default engine is SQLite for local development; swap the URL in db.py
for Postgres in production without touching this file.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    JSON,
    String,
    Index,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


# ---------------------------------------------------------------------------
# Tier 1: Intraday news & sentiment (interval-based, accumulates through the day)
# ---------------------------------------------------------------------------
class NewsSentiment(Base):
    __tablename__ = "news_sentiment"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False, index=True)
    fetched_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    published_at = Column(DateTime, nullable=True)
    headline = Column(String, nullable=True)
    source = Column(String, nullable=True)
    sentiment_score = Column(Float, nullable=True)       # e.g. Alpha Vantage's ticker_sentiment_score
    relevance_score = Column(Float, nullable=True)
    topics = Column(JSON, nullable=True)                 # e.g. ["mergers_and_acquisitions", "earnings"]
    raw = Column(JSON, nullable=True)                     # full provider payload, for audit/replay

    __table_args__ = (Index("ix_news_symbol_time", "symbol", "published_at"),)


# ---------------------------------------------------------------------------
# Tier 2: Daily one-time reference data
# ---------------------------------------------------------------------------
class InsiderTransaction(Base):
    __tablename__ = "insider_transactions"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False, index=True)
    fetched_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    transaction_date = Column(DateTime, nullable=True)
    insider_name = Column(String, nullable=True)
    title = Column(String, nullable=True)
    transaction_type = Column(String, nullable=True)      # buy / sell / option exercise, etc.
    shares = Column(Float, nullable=True)
    price = Column(Float, nullable=True)
    raw = Column(JSON, nullable=True)


class CongressTrade(Base):
    __tablename__ = "congress_trades"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False, index=True)
    fetched_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    transaction_date = Column(DateTime, nullable=True)
    bioguide_id = Column(String, nullable=True, index=True)
    chamber = Column(String, nullable=True)                # house / senate
    transaction_type = Column(String, nullable=True)
    amount_range = Column(String, nullable=True)
    raw = Column(JSON, nullable=True)


class PoliticianMetadata(Base):
    """
    Reference/biographical data — changes rarely. Fetched daily by default
    per the current schedule, but the natural optimization later is
    "fetch once per bioguide_id, refresh only on a cache miss."
    """

    __tablename__ = "politician_metadata"

    bioguide_id = Column(String, primary_key=True)
    name = Column(String, nullable=True)
    chamber = Column(String, nullable=True)
    party = Column(String, nullable=True)
    state = Column(String, nullable=True)
    fetched_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    raw = Column(JSON, nullable=True)


class InstitutionalHolding(Base):
    __tablename__ = "institutional_holdings"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False, index=True)
    fetched_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    report_date = Column(DateTime, nullable=True)
    institution_name = Column(String, nullable=True)
    shares_held = Column(Float, nullable=True)
    value = Column(Float, nullable=True)
    change_pct = Column(Float, nullable=True)
    raw = Column(JSON, nullable=True)


# ---------------------------------------------------------------------------
# Tier 3: Event-triggered fundamentals
# ---------------------------------------------------------------------------
class Fundamentals(Base):
    """
    One row per (symbol, fiscal_date_ending) snapshot. `valid_until` is the
    next expected earnings date; `is_dirty` is flipped by the corporate-
    action watcher (M&A news topic hit, new 8-K filing, etc.) to force an
    early refresh regardless of `valid_until`.
    """

    __tablename__ = "fundamentals"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False, index=True)
    fetched_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    fiscal_date_ending = Column(DateTime, nullable=True)
    valid_until = Column(DateTime, nullable=True)          # next expected earnings date
    is_dirty = Column(Boolean, nullable=False, default=False)
    dirty_reason = Column(String, nullable=True)            # e.g. "news:mergers_and_acquisitions", "filing:8-K"
    income_statement = Column(JSON, nullable=True)
    balance_sheet = Column(JSON, nullable=True)
    cash_flow = Column(JSON, nullable=True)
    earnings = Column(JSON, nullable=True)

    __table_args__ = (Index("ix_fundamentals_symbol_latest", "symbol", "fetched_at"),)


class CorporateActionFlag(Base):
    """
    Append-only log of things that triggered (or will trigger) an
    out-of-cycle fundamentals refresh. Keeping this separate from
    Fundamentals.is_dirty gives you an audit trail of *why* a refresh fired.
    """

    __tablename__ = "corporate_action_flags"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False, index=True)
    flagged_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    reason = Column(String, nullable=False)                 # "earnings_calendar_passed" | "news_topic" | "8k_filing"
    source_ref = Column(String, nullable=True)               # article id / filing accession number
    resolved = Column(Boolean, nullable=False, default=False)


# ---------------------------------------------------------------------------
# Tier 4: Technical, core stock, index, and options data (shared interval)
# ---------------------------------------------------------------------------
class TechnicalSnapshot(Base):
    __tablename__ = "technical_snapshots"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False, index=True)
    fetched_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    timeframe = Column(String, nullable=True)                # "15m", "1h", "1d", etc.
    ohlcv = Column(JSON, nullable=True)
    indicators = Column(JSON, nullable=True)                 # RSI, MACD, ADX, SMC levels, etc.

    __table_args__ = (Index("ix_technical_symbol_time", "symbol", "fetched_at"),)


class IndexSnapshot(Base):
    __tablename__ = "index_snapshots"

    id = Column(Integer, primary_key=True)
    index_symbol = Column(String, nullable=False, index=True)
    fetched_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    ohlcv = Column(JSON, nullable=True)


class OptionSnapshot(Base):
    __tablename__ = "option_snapshots"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False, index=True)
    fetched_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expiration = Column(DateTime, nullable=True)
    chain = Column(JSON, nullable=True)


# ---------------------------------------------------------------------------
# Agent + skill output log (feeds the backtesting / weight-tuning loop)
# ---------------------------------------------------------------------------
class AgentSignal(Base):
    __tablename__ = "agent_signals"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False, index=True)
    run_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    agent_name = Column(String, nullable=False)              # "technical" | "fundamental" | "sentiment" | "macro"
    direction = Column(String, nullable=False)                # "bullish" | "bearish" | "neutral"
    confidence = Column(Float, nullable=False)
    rationale = Column(String, nullable=True)
    key_signals = Column(JSON, nullable=True)
    available = Column(Boolean, nullable=False, default=True)  # False if this agent timed out/errored this run


class FinalSignal(Base):
    __tablename__ = "final_signals"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False, index=True)
    run_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    skill_name = Column(String, nullable=False)               # "swing_trade_scanner" | "catalyst_confluence_scanner"
    direction = Column(String, nullable=False)
    confidence = Column(Float, nullable=False)
    composite_score = Column(Float, nullable=True)
    contributing_agents = Column(JSON, nullable=True)          # {"technical": 0.4, "sentiment": 0.25, ...}
    # Filled in later once the trade plays out, to power weight-tuning:
    outcome_checked_at = Column(DateTime, nullable=True)
    outcome_correct = Column(Boolean, nullable=True)


# ---------------------------------------------------------------------------
# Runtime config — lets the frontend change parameters (watchlist, tier
# cadences) without a redeploy. Seeded from config/schedule.yaml on first
# read; see store/config_store.py.
# ---------------------------------------------------------------------------
class AppConfig(Base):
    __tablename__ = "app_config"

    key = Column(String, primary_key=True)
    value = Column(JSON, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
