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
from typing import Any, Dict, Iterable, Optional

import pandas as pd
import yfinance as yf

from store.db import get_session
from store.models import CorporateActionFlag, Fundamentals, NewsSentiment

logger = logging.getLogger(__name__)

MA_TOPIC = "mergers_and_acquisitions"

# yfinance Ticker.info key -> flattened column label, matching the metrics
# actually needed for fundamental analysis (not full statements). Growth
# rates for Free Cash Flow and EPS aren't single-snapshot .info fields —
# see _yoy_growth() below, computed separately from quarterly history.
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
    "EPS (Trailing)": "trailingEps",
    "EPS (Forward)": "forwardEps",
    "Free Cash Flow": "freeCashflow",
}

# The five factors the fundamental health score ranks on, and where each
# one lives in the metrics dict built below. Equal-weighted by default;
# a factor missing for a given symbol (e.g. no quarterly cash flow history
# yet) is excluded and the rest renormalized, same redistribution pattern
# as orchestrator/composite.py uses for a missing agent.
HEALTH_SCORE_FACTORS = [
    "Free Cash Flow Growth (YoY)",
    "EPS Growth (YoY)",
    "Revenue Growth (YoY)",
    "Quarterly Earnings Growth (YoY)",
    "Forward P/E Decline (%)",
]

# Same neutral-band convention as NewsSentiment's sentiment_score_definition
# and orchestrator/composite.py's bullish/bearish thresholds, extended to a
# 5-tier rank since "how strong or healthy" calls for more than a 3-way
# split.
_HEALTH_RANK_BANDS = [
    (0.35, "Strong"),
    (0.15, "Somewhat Strong"),
    (-0.15, "Neutral"),
    (-0.35, "Somewhat Weak"),
]

# Standard market-cap tiers. The user's spec repeated "Micro" for both the
# $50mn-$300mn band and "less than $50mn" — that's the standard finance
# convention's Micro/Nano split with the Nano label just missing, so this
# uses Nano for the bottom band rather than two tiers both called Micro.
_MARKET_CAP_TIERS = [
    (200_000_000_000, "Mega"),
    (10_000_000_000, "Large"),
    (2_000_000_000, "Mid"),
    (300_000_000, "Small"),
    (50_000_000, "Micro"),
]


def _market_cap_tier(market_cap: Optional[float]) -> Optional[str]:
    if market_cap is None:
        return None
    for threshold, label in _MARKET_CAP_TIERS:
        if market_cap >= threshold:
            return label
    return "Nano"


def _yoy_growth(series: "pd.Series") -> Optional[float]:
    """
    Given a time series indexed by period-end date (most recent first —
    yfinance's own convention for quarterly_cashflow columns and
    get_earnings_dates() rows), return the fractional YoY growth between
    the latest value and whichever earlier entry falls closest to exactly
    one year before it. Returns None if there's under a year of history,
    the closest match is more than ~2 months off target (too sparse a
    history to trust as "a year ago"), or the prior value is zero.
    """
    series = series.dropna()
    if len(series) < 2:
        return None
    latest_date, latest_val = series.index[0], series.iloc[0]
    target = latest_date - pd.DateOffset(years=1)
    closest = min(series.index[1:], key=lambda d: abs((d - target).days))
    if abs((closest - target).days) > 60:
        return None
    prior_val = series.loc[closest]
    if not prior_val:
        return None
    return (latest_val - prior_val) / abs(prior_val)


def _fcf_growth_yoy(ticker: "yf.Ticker") -> Optional[float]:
    try:
        qcf = ticker.quarterly_cashflow
        if qcf is None or "Free Cash Flow" not in qcf.index:
            return None
        return _yoy_growth(qcf.loc["Free Cash Flow"])
    except Exception as e:
        logger.debug("quarterly_cashflow failed: %s: %s", type(e).__name__, e)
        return None


def _eps_growth_yoy(ticker: "yf.Ticker") -> Optional[float]:
    try:
        earnings_dates = ticker.get_earnings_dates(limit=8)
        if earnings_dates is None or "Reported EPS" not in earnings_dates.columns:
            return None
        return _yoy_growth(earnings_dates["Reported EPS"])
    except Exception as e:
        logger.debug("get_earnings_dates() failed: %s: %s", type(e).__name__, e)
        return None


def _forward_pe_decline(metrics: Dict[str, Any]) -> Optional[float]:
    """Fraction by which forward P/E sits below trailing P/E — a positive
    value means the market is pricing in enough earnings growth to compress
    the multiple even as the price holds (cheaper on a forward basis, all
    else equal); negative means the opposite. Needs no extra fetch — both
    P/E values are already in `metrics` from Ticker.info."""
    trailing, forward = metrics.get("Trailing P/E"), metrics.get("Forward P/E")
    if not trailing or forward is None:
        return None
    return (trailing - forward) / trailing


def compute_health_score(metrics: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Equal-weighted average of the five HEALTH_SCORE_FACTORS already present
    in `metrics` (Free Cash Flow Growth, EPS Growth, Revenue Growth,
    Quarterly Earnings Growth, Forward P/E Decline), redistributing weight
    away from whichever factors are missing rather than treating them as
    zero. Returns None if every factor is missing — nothing to score.
    """
    available = {name: metrics[name] for name in HEALTH_SCORE_FACTORS if metrics.get(name) is not None}
    if not available:
        return None

    score = sum(available.values()) / len(available)
    rank = next((label for threshold, label in _HEALTH_RANK_BANDS if score >= threshold), "Weak")

    return {"score": score, "rank": rank, "factors_used": list(available.keys())}


def _build_metrics(ticker: "yf.Ticker", symbol: str) -> Dict[str, Any]:
    """
    The full metrics dict for one symbol, given an already-constructed
    Ticker: everything in METRIC_FIELDS from Ticker.info, plus the two
    growth rates that need separate historical calls (Free Cash Flow
    Growth, EPS Growth), plus the derived Forward P/E Decline, plus the
    resulting fundamental health score/rank (Fundamental Health Score /
    Fundamental Health Rank) — see compute_health_score(). Takes a Ticker
    rather than a symbol so callers that also need e.g. _next_earnings_date
    can share one instance — yfinance caches each property per instance,
    so reusing it (rather than constructing a fresh Ticker per helper)
    is what keeps this at one network call per distinct piece of data
    instead of two.
    """
    info = ticker.info or {}
    metrics = {"Ticker": symbol}
    for label, key in METRIC_FIELDS.items():
        metrics[label] = info.get(key)

    metrics["Market Cap Tier"] = _market_cap_tier(metrics.get("Market Capitalization"))
    metrics["Free Cash Flow Growth (YoY)"] = _fcf_growth_yoy(ticker)
    metrics["EPS Growth (YoY)"] = _eps_growth_yoy(ticker)
    metrics["Forward P/E Decline (%)"] = _forward_pe_decline(metrics)

    health = compute_health_score(metrics)
    metrics["Fundamental Health Score"] = health["score"] if health else None
    metrics["Fundamental Health Rank"] = health["rank"] if health else None

    return metrics


# ---------------------------------------------------------------------------
# Display formatting — the stored `metrics` values stay raw (full-precision
# floats) since agents/fundamental.py's prompt wants those, not strings.
# This layer is purely for rendering: percentages for growth/margin rates,
# "x" ratios for P/E-style multiples, short-scale currency for large
# dollar figures, plain currency for per-share values, and "pts" for the
# composite health score (same 0-100ish scale as a percentage, labeled
# differently since it's a blended index rather than one real growth rate).
# ---------------------------------------------------------------------------


def _fmt_currency_short(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    sign, value = ("-", -value) if value < 0 else ("", value)
    for threshold, suffix in [(1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")]:
        if value >= threshold:
            return f"{sign}${value / threshold:.2f}{suffix}"
    return f"{sign}${value:.2f}"


def _fmt_currency(value: Optional[float]) -> Optional[str]:
    return f"${value:,.2f}" if value is not None else None


def _fmt_ratio(value: Optional[float]) -> Optional[str]:
    return f"{value:.2f}x" if value is not None else None


def _fmt_plain(value: Optional[float]) -> Optional[str]:
    return f"{value:.2f}" if value is not None else None


def _fmt_percent(value: Optional[float]) -> Optional[str]:
    """For fields already expressed as a 0-1 fraction (growth rates, margins)."""
    return f"{value * 100:.1f}%" if value is not None else None


def _fmt_percent_already(value: Optional[float]) -> Optional[str]:
    """For fields yfinance already returns in percentage-point units — verified
    live against dividendYield (T=4.32, VZ=5.64: real yields, not 0-1 fractions
    that would imply 432%/564%)."""
    return f"{value:.2f}%" if value is not None else None


def _fmt_points(value: Optional[float]) -> Optional[str]:
    return f"{value * 100:.1f} pts" if value is not None else None


# Field label -> formatter. None means the field is already display-ready
# text (a tier or rank label) and passes through unchanged.
FIELD_FORMATTERS = {
    "Market Capitalization": _fmt_currency_short,
    "Market Cap Tier": None,
    "Trailing P/E": _fmt_ratio,
    "Forward P/E": _fmt_ratio,
    "Price-to-Book (P/B)": _fmt_ratio,
    "Beta (5Y Monthly)": _fmt_plain,
    "Dividend Yield": _fmt_percent_already,
    "Revenue Growth (YoY)": _fmt_percent,
    "Quarterly Earnings Growth (YoY)": _fmt_percent,
    "Profit Margin": _fmt_percent,
    "Operating Margin": _fmt_percent,
    "EPS (Trailing)": _fmt_currency,
    "EPS (Forward)": _fmt_currency,
    "Free Cash Flow": _fmt_currency_short,
    "Free Cash Flow Growth (YoY)": _fmt_percent,
    "EPS Growth (YoY)": _fmt_percent,
    "Forward P/E Decline (%)": _fmt_percent,
    "Fundamental Health Score": _fmt_points,
    "Fundamental Health Rank": None,
}


def format_metrics_for_display(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Formatted-for-display copy of a metrics dict — percentages, ratios,
    short-scale currency, points — per FIELD_FORMATTERS. Used by the
    dashboard; the stored/prompted `metrics` dict itself stays raw."""
    formatted = {}
    for key, value in metrics.items():
        if key == "Ticker":
            formatted[key] = value
            continue
        formatter = FIELD_FORMATTERS.get(key)
        formatted[key] = formatter(value) if formatter else value
    return formatted


def fetch_fundamental_metrics(symbol: str) -> Dict[str, Any]:
    """
    Standalone convenience wrapper around _build_metrics() for a symbol on
    its own — constructs its own Ticker. Unlike Alpha Vantage's
    EARNINGS_CALENDAR, yfinance has no multi-ticker batch endpoint, so
    none of this can be combined across symbols the way Tier 1's
    NEWS_SENTIMENT call is.
    """
    return _build_metrics(yf.Ticker(symbol), symbol)


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
    metrics = _build_metrics(ticker, symbol)

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
