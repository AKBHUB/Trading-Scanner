"""
Fundamental agent — reads the current (non-stale) Fundamentals row for a
symbol, written by ingestion/fundamentals.py (yfinance-backed: market cap,
P/E, P/B, beta, dividend yield, growth rates, margins, plus a computed
fundamental health score/rank — see compute_health_score() there).

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
You are a fundamental analysis agent. Weigh these five factors above all
else, in this order, as the primary basis for how strong or healthy the
stock is fundamentally:

1. Free Cash Flow Growth (YoY)
2. EPS Growth (YoY)
3. Revenue Growth (YoY)
4. Quarterly Earnings Growth (YoY)
5. Forward P/E Decline (%) — how much cheaper the forward P/E is than the
   trailing P/E; a positive value means the market expects enough
   earnings growth to compress the multiple, which is a bullish signal,
   not bearish.

You're also given a pre-computed Fundamental Health Score (an
equal-weighted average of whichever of those five factors are available)
and its Fundamental Health Rank label (Strong / Somewhat Strong / Neutral
/ Somewhat Weak / Weak). Treat that score as a strong prior — your own
direction and confidence should usually agree with it — but you may
diverge if the supporting valuation/margin metrics (P/E, P/B, margins)
clearly contradict what the five growth/valuation factors imply on their
own. Explain any divergence in your rationale.
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

    metrics = snapshot.metrics or {}
    summary = (
        f"Symbol: {symbol}\n"
        f"Fundamental Health Score: {metrics.get('Fundamental Health Score')}\n"
        f"Fundamental Health Rank: {metrics.get('Fundamental Health Rank')}\n"
        f"Free Cash Flow Growth (YoY): {metrics.get('Free Cash Flow Growth (YoY)')}\n"
        f"EPS Growth (YoY): {metrics.get('EPS Growth (YoY)')}\n"
        f"Revenue Growth (YoY): {metrics.get('Revenue Growth (YoY)')}\n"
        f"Quarterly Earnings Growth (YoY): {metrics.get('Quarterly Earnings Growth (YoY)')}\n"
        f"Forward P/E Decline (%): {metrics.get('Forward P/E Decline (%)')}\n"
        f"All metrics: {metrics}"
    )

    try:
        result = call_claude(SYSTEM_PROMPT, summary)
        persist_signal(symbol, "fundamental", result)
    except Exception as e:
        persist_signal(symbol, "fundamental", None, available=False, error=f"{type(e).__name__}: {e}")
