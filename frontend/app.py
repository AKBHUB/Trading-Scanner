"""
Operator dashboard for Trading-Scanner: set watchlist/schedule
parameters, trigger ingestion tiers and agents on demand, and see the
latest stored signals — all backed by the same store/ingestion/agents
package the scheduled worker uses. No separate API layer; this app
imports those modules directly.

Run locally:
    streamlit run frontend/app.py

On Railway, this is the "web" service; ingestion/scheduler.py runs as
a separate "worker" service, both pointed at the same DATABASE_URL.
"""

import os
import sys

# Streamlit executes this file directly, which puts frontend/ on
# sys.path instead of the repo root — add the repo root explicitly so
# the store/ingestion/agents packages import correctly regardless of
# the container's working directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta

import streamlit as st

st.set_page_config(page_title="Trading Scanner", page_icon="📈", layout="wide", initial_sidebar_state="expanded")

from agents import fundamental, macro, sentiment, technical
from ingestion import daily_reference, fundamentals, news_sentiment, technical_core
from ingestion.scheduler import start_scheduler_background
from orchestrator import composite
from store.config_store import get_config, set_config
from store.db import get_session, init_db
from store.models import AgentSignal, CongressTrade, FinalSignal, Fundamentals, NewsSentiment, TechnicalSnapshot

st.markdown(
    """
    <style>
    [data-testid="stAppViewContainer"] { background: #f5f7fb; }
    [data-testid="stHeader"] { background: rgba(245,247,251,.92); }
    .block-container { max-width: 1500px; padding-top: 2rem; }
    .hero { background: linear-gradient(120deg, #102a43, #1f5f8b); color: white;
            border-radius: 12px; padding: 1.35rem 1.6rem; margin-bottom: 1.1rem; }
    .hero h1 { margin: 0; font-size: 2rem; letter-spacing: 0; }
    .hero p { margin: .35rem 0 0; color: #d9ecff; }
    </style>
    """,
    unsafe_allow_html=True,
)

startup_error = None
try:
    init_db()
except Exception as exc:
    startup_error = f"Database startup failed: {type(exc).__name__}: {exc}"

# Starts the APScheduler loop in a background thread of this same process,
# so this one Streamlit service both serves the dashboard and drives
# scheduled ingestion — no separate worker process to deploy. Idempotent:
# Streamlit reruns this whole script on every interaction, but the second
# call onward just returns the already-running scheduler instance.
_scheduler = None
if startup_error is None:
    try:
        _scheduler = start_scheduler_background()
    except Exception as exc:
        startup_error = f"Scheduler startup failed: {type(exc).__name__}: {exc}"

st.markdown(
    '<section class="hero"><h1>Trading Scanner</h1><p>Market context, agent calls, and actionable signals in one operating view.</p></section>',
    unsafe_allow_html=True,
)

if startup_error:
    st.error(startup_error)
    st.info("Fix the configuration shown above, then reload the app. The dashboard has not started background jobs.")

news_count = technical_count = signal_count = 0
if startup_error is None:
    with get_session() as session:
        news_count = session.query(NewsSentiment).count()
        technical_count = session.query(TechnicalSnapshot).count()
        signal_count = session.query(FinalSignal).count()

with st.sidebar:
    st.subheader("System health")
    if _scheduler is None:
        st.error("Scheduler unavailable")
    else:
        st.success("Scheduler running" if _scheduler.running else "Scheduler stopped")
        jobs = sorted(_scheduler.get_jobs(), key=lambda j: j.next_run_time or datetime.max)
        for job in jobs:
            next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M %Z") if job.next_run_time else "—"
            st.caption(f"{job.func.__name__}: {next_run}")

watchlist = get_config("watchlist", [])
metric_cols = st.columns(4)
metric_cols[0].metric("Watchlist", len(watchlist), help="Symbols currently used by scheduled tiers.")
metric_cols[1].metric("News rows", news_count)
metric_cols[2].metric("Technical snapshots", technical_count)
metric_cols[3].metric("Composite signals", signal_count)

tab_config, tab_run, tab_fundamentals, tab_signals = st.tabs(
    ["Watchlist & Schedule", "Run Now", "Fundamentals", "Signals"]
)

# ---------------------------------------------------------------------------
# Watchlist & schedule
# ---------------------------------------------------------------------------
with tab_config:
    st.subheader("Watchlist")
    watchlist = get_config("watchlist", [])
    watchlist_text = st.text_area("Symbols (comma-separated)", value=", ".join(watchlist), height=68)
    index_watchlist = get_config("index_watchlist", [])
    index_text = st.text_input("Index symbols (comma-separated)", value=", ".join(index_watchlist))

    if st.button("Save watchlist"):
        set_config("watchlist", [s.strip().upper() for s in watchlist_text.split(",") if s.strip()])
        set_config("index_watchlist", [s.strip().upper() for s in index_text.split(",") if s.strip()])
        st.success("Saved — takes effect on each tier's next run, no restart needed.")

    st.divider()
    st.subheader("Tier schedule")
    st.caption("Saved immediately, but the worker only picks up new cadences on its next restart.")

    tiers_cfg = get_config("tiers", {})
    news_cfg = tiers_cfg.get("news_sentiment", {})
    tech_cfg = tiers_cfg.get("technical_core", {})
    fundamental_cfg = tiers_cfg.get("fundamentals", {})
    daily_cfg = tiers_cfg.get("daily_reference", {}).get("jobs", {})
    reference_cfg = tiers_cfg.get("daily_reference", {})

    col1, col2 = st.columns(2)
    with col1:
        fundamental_time = st.text_input(
            "Tier 1 fundamental extraction (HH:MM)", value=fundamental_cfg.get("extraction_time", "06:30")
        )
        news_interval = st.number_input(
            "News/sentiment interval (minutes)", min_value=5, value=int(news_cfg.get("interval_minutes", 30))
        )
        news_start = st.text_input("News window start (HH:MM)", value=news_cfg.get("start_time", "08:30"))
        news_end = st.text_input("News window end (HH:MM)", value=news_cfg.get("end_time", "15:15"))
    with col2:
        technical_time = st.text_input(
            "Tier 4 baseline (HH:MM)", value=tech_cfg.get("baseline_time", "07:30")
        )

    st.caption("High-volume daily reference filters (minimum transaction or holding value)")
    f1, f2, f3 = st.columns(3)
    insider_min = f1.number_input(
        "Insider minimum ($)", min_value=0, value=int(reference_cfg.get("min_insider_transaction_value", 100000))
    )
    congress_min = f2.number_input(
        "Congress minimum ($)", min_value=0, value=int(reference_cfg.get("min_congress_transaction_value", 15000))
    )
    institutional_min = f3.number_input(
        "Institutional minimum ($)", min_value=0, value=int(reference_cfg.get("min_institutional_holding_value", 1000000))
    )

    st.caption("Daily one-time job times (HH:MM)")
    d1, d2, d3, d4 = st.columns(4)
    insider_time = d1.text_input("Insider transactions", value=daily_cfg.get("insider_transactions", "07:00"))
    congress_time = d2.text_input("Congress trades", value=daily_cfg.get("congress_trades", "07:05"))
    inst_time = d3.text_input("Institutional holdings", value=daily_cfg.get("institutional_holdings", "07:10"))
    politician_time = d4.text_input("Politician metadata", value=daily_cfg.get("politician_metadata", "07:15"))

    if st.button("Save schedule"):
        set_config(
            "tiers",
            {
                "fundamentals": {
                    "type": "daily",
                    "extraction_time": fundamental_time,
                    "watchlist_source": "watchlist",
                },
                "news_sentiment": {
                    "interval_minutes": news_interval,
                    "start_time": news_start,
                    "end_time": news_end,
                },
                "technical_core": {
                    "type": "daily_plus_catalyst_event",
                    "baseline_time": technical_time,
                    "event_sources": ["news_sentiment", "daily_reference"],
                    "market_hours_only": True,
                    "includes": ["ohlcv", "indicators", "index", "options"],
                },
                "daily_reference": {
                    "type": "daily",
                    "min_insider_transaction_value": insider_min,
                    "min_congress_transaction_value": congress_min,
                    "min_institutional_holding_value": institutional_min,
                    "jobs": {
                        "insider_transactions": insider_time,
                        "congress_trades": congress_time,
                        "institutional_holdings": inst_time,
                        "politician_metadata": politician_time,
                    }
                },
                "fundamentals": tiers_cfg.get("fundamentals", {}),
            },
        )
        st.success("Saved — restart the worker service on Railway to apply new cadences.")

# ---------------------------------------------------------------------------
# Run now
# ---------------------------------------------------------------------------
with tab_run:
    st.subheader("Ingestion tiers")
    watchlist = get_config("watchlist", [])
    index_watchlist = get_config("index_watchlist", [])
    if watchlist:
        st.caption(f"Current watchlist: {', '.join(watchlist)}")
    else:
        st.caption(
            "No watchlist saved — news & sentiment will pull Alpha Vantage's "
            "general top-financial-news feed instead of per-ticker; the other "
            "tiers below need an actual watchlist and will write nothing until "
            "you set one."
        )

    c1, c2, c3, c4 = st.columns(4)
    if c1.button("Run news & sentiment"):
        with st.spinner("Fetching news & sentiment..."):
            count = news_sentiment.run(watchlist)
        st.success(f"Wrote {count} rows.")

    if c2.button("Run daily reference"):
        with st.spinner("Fetching insider, congress, institutional, politician data..."):
            insider_n = daily_reference.run_insider_transactions(watchlist)
            congress_n = daily_reference.run_congress_trades(watchlist)
            holdings_n = daily_reference.run_institutional_holdings(watchlist)
            with get_session() as session:
                bioguide_ids = {
                    r.bioguide_id for r in session.query(CongressTrade.bioguide_id).distinct() if r.bioguide_id
                }
            politician_n = daily_reference.run_politician_metadata(bioguide_ids)
        st.success(f"Insider: {insider_n}, congress: {congress_n}, institutional: {holdings_n}, politicians: {politician_n}")

    if c3.button("Run Tier 1 fundamentals"):
        with st.spinner("Extracting fundamentals for the watchlist..."):
            count = fundamentals.refresh_all(watchlist)
        st.success(f"Refreshed {count} watchlist symbol(s).")

    if c4.button("Run technical & core"):
        with st.spinner("Fetching technical indicators, OHLCV, index, options..."):
            technical_core.run_technical(watchlist)
            technical_core.run_index_data(index_watchlist)
        st.success("Technical & core tier run complete.")

    st.divider()
    st.subheader("Agents")
    # .strip().upper(): the watchlist-derived symbols are already uppercase
    # (set_config normalizes them on save), but this free-text fallback —
    # used whenever no watchlist is set — wasn't, so a manually-typed
    # lowercase symbol would create separate, inconsistent store rows from
    # the uppercase ones every other tier/agent writes.
    symbol = st.selectbox("Symbol", watchlist) if watchlist else st.text_input("Symbol").strip().upper()

    a1, a2, a3, a4 = st.columns(4)
    # Buttons must always render regardless of whether `symbol` is set —
    # `if symbol and a1.button(...)` short-circuited and skipped calling
    # a1.button() entirely whenever symbol was empty (no watchlist saved,
    # nothing typed yet), which made the buttons vanish from the page
    # rather than just being unusable. Render unconditionally; check
    # `symbol` only when deciding whether a click should do anything.
    run_technical = a1.button("Run technical agent")
    run_fundamental = a2.button("Run fundamental agent")
    run_sentiment = a3.button("Run sentiment agent")
    run_macro = a4.button("Run macro agent")

    if (run_technical or run_fundamental or run_sentiment or run_macro) and not symbol:
        st.warning("Enter or select a symbol above first.")
    else:
        if run_technical:
            with st.spinner(f"Analyzing {symbol}..."):
                technical.analyze(symbol)
            st.success("Done — see Recent Signals tab.")
        if run_fundamental:
            with st.spinner(f"Analyzing {symbol}..."):
                fundamental.analyze(symbol)
            st.success("Done — see Recent Signals tab.")
        if run_sentiment:
            with st.spinner(f"Analyzing {symbol}..."):
                sentiment.analyze(symbol)
            st.success("Done — see Recent Signals tab.")
        if run_macro:
            with st.spinner(f"Analyzing {symbol}..."):
                macro.analyze(symbol, index_watchlist)
            st.success("Done — see Signals tab.")

        st.caption("Or run all four and combine them into one weighted call:")
        if st.button("Run all agents + compute composite", type="primary"):
            if not symbol:
                st.warning("Enter or select a symbol above first.")
            else:
                with st.spinner(f"Running all four agents for {symbol}..."):
                    technical.analyze(symbol)
                    fundamental.analyze(symbol)
                    sentiment.analyze(symbol)
                    macro.analyze(symbol, index_watchlist)
                    final = composite.compute_composite(symbol)
                if final is None:
                    st.error("No usable agent signal to combine — every agent came back unavailable.")
                else:
                    st.success(
                        f"Composite for {symbol}: **{final.direction.upper()}** "
                        f"(confidence {final.confidence:.2f}, score {final.composite_score:+.2f}) — see Signals tab."
                    )
                    st.json(final.contributing_agents)

# ---------------------------------------------------------------------------
# Fundamentals — yfinance-backed metrics, its own ticker list + quick refresh
# ---------------------------------------------------------------------------
with tab_fundamentals:
    st.subheader("Fundamentals ticker list")
    st.caption(
        "Optional — separate from the main watchlist above. Leave empty to "
        "have the fundamentals tier (event-triggered + weekly safety-net "
        "refresh, config/schedule.yaml) just use the main watchlist instead."
    )
    fund_watchlist = get_config("fundamentals_watchlist", [])
    fund_watchlist_text = st.text_area(
        "Symbols (comma-separated)", value=", ".join(fund_watchlist), height=68, key="fund_watchlist_text"
    )
    if st.button("Save fundamentals ticker list"):
        set_config("fundamentals_watchlist", [s.strip().upper() for s in fund_watchlist_text.split(",") if s.strip()])
        st.success("Saved — takes effect on the next scheduled fundamentals run, no restart needed.")

    effective_fund_watchlist = fund_watchlist or get_config("watchlist", [])
    st.caption(
        f"Effective list right now: {', '.join(effective_fund_watchlist) or '(empty — set a watchlist first)'}"
    )

    st.divider()
    if st.button("Quick refresh", type="primary"):
        if not effective_fund_watchlist:
            st.warning("No tickers to refresh — set a fundamentals or main watchlist first.")
        else:
            with st.spinner(f"Fetching fundamentals for {len(effective_fund_watchlist)} symbol(s) via yfinance..."):
                count = fundamentals.refresh_all(effective_fund_watchlist)
            st.success(f"Refreshed {count} of {len(effective_fund_watchlist)} symbol(s).")

    st.divider()
    st.subheader("Fundamental health ranking")
    st.caption(
        "Ranked by Fundamental Health Score — an equal-weighted average of Free "
        "Cash Flow Growth, EPS Growth, Revenue Growth, Quarterly Earnings Growth, "
        "and Forward P/E Decline (a factor missing for a symbol is excluded and "
        "the rest renormalized, not treated as zero)."
    )
    with get_session() as session:
        rows = session.query(Fundamentals).order_by(Fundamentals.fetched_at.desc()).all()

    latest_by_symbol = {}
    for row in rows:
        latest_by_symbol.setdefault(row.symbol, row)

    if not latest_by_symbol:
        st.info("No fundamentals fetched yet — use \"Quick refresh\" above.")
    else:
        # Sort by the raw score (None sorts last), then format for display —
        # formatted strings like "22.6 pts" don't sort correctly as text.
        ranked = sorted(
            latest_by_symbol.values(),
            key=lambda f: (f.metrics or {}).get("Fundamental Health Score")
            if (f.metrics or {}).get("Fundamental Health Score") is not None
            else float("-inf"),
            reverse=True,
        )
        st.dataframe(
            [
                {
                    "rank": i + 1,
                    "symbol": f.symbol,
                    "health_score": fmt.get("Fundamental Health Score"),
                    "health_rank": fmt.get("Fundamental Health Rank"),
                    "market_cap": fmt.get("Market Capitalization"),
                    "market_cap_tier": fmt.get("Market Cap Tier"),
                    "fcf_growth_yoy": fmt.get("Free Cash Flow Growth (YoY)"),
                    "eps_growth_yoy": fmt.get("EPS Growth (YoY)"),
                    "revenue_growth_yoy": fmt.get("Revenue Growth (YoY)"),
                    "earnings_growth_yoy": fmt.get("Quarterly Earnings Growth (YoY)"),
                    "forward_pe_decline_pct": fmt.get("Forward P/E Decline (%)"),
                    "forward_pe_health": fmt.get("Forward P/E Health"),
                    "peg_ratio": fmt.get("PEG Ratio"),
                    "peg_valuation": fmt.get("PEG Valuation"),
                    "fetched_at": f.fetched_at,
                }
                for i, f in enumerate(ranked)
                for fmt in [fundamentals.format_metrics_for_display(f.metrics or {})]
            ],
            width="stretch",
        )

        st.divider()
        st.subheader("All metrics per symbol")
        st.caption(
            "Formatted for display — percentages for growth/margin rates, "
            "\"x\" for P/E-style ratios, short-scale currency for large "
            "dollar figures, points for the composite health score. "
            "Free Cash Flow Growth (YoY) is shown as Free Cash Flow Trend "
            "here — the raw % still feeds the health score and appears in "
            "the ranking table above."
        )
        st.dataframe(
            [
                {
                    "symbol": f.symbol,
                    **{
                        k: v
                        for k, v in fundamentals.format_metrics_for_display(f.metrics or {}).items()
                        if k not in ("Ticker", "Free Cash Flow Growth (YoY)")
                    },
                    "next_earnings": f.valid_until,
                    "fetched_at": f.fetched_at,
                    "dirty_reason": f.dirty_reason,
                }
                for f in sorted(latest_by_symbol.values(), key=lambda f: f.symbol)
            ],
            width="stretch",
        )

# ---------------------------------------------------------------------------
# Signals — composite call per symbol, plus raw per-agent detail
# ---------------------------------------------------------------------------
with tab_signals:
    st.subheader("Composite signal (latest per symbol × skill)")
    st.caption(
        "swing_trade_scanner and catalyst_confluence_scanner run on their own "
        "schedule (config/schedule.yaml); composite_orchestrator is whatever "
        "you last triggered manually from Run Now — each is shown separately "
        "per symbol rather than one hiding the others."
    )
    with get_session() as session:
        final_rows = session.query(FinalSignal).order_by(FinalSignal.run_at.desc()).all()

    latest_final_by_key = {}
    for row in final_rows:
        latest_final_by_key.setdefault((row.symbol, row.skill_name), row)

    if not latest_final_by_key:
        st.info("No composite signals yet — use \"Run all agents + compute composite\" in the Run Now tab.")
    else:
        st.dataframe(
            [
                {
                    "symbol": f.symbol,
                    "skill": f.skill_name,
                    "direction": f.direction,
                    "confidence": round(f.confidence, 3),
                    "composite_score": round(f.composite_score, 3) if f.composite_score is not None else None,
                    "contributing_agents": f.contributing_agents,
                    "run_at": f.run_at,
                }
                for f in sorted(latest_final_by_key.values(), key=lambda f: (f.symbol, f.skill_name))
            ],
            width="stretch",
        )

    st.divider()
    st.subheader("Per-agent signal detail")
    with get_session() as session:
        rows = session.query(AgentSignal).order_by(AgentSignal.run_at.desc()).limit(50).all()

    if not rows:
        st.info("No agent signals yet — run an agent from the Run Now tab.")
    else:
        st.dataframe(
            [
                {
                    "symbol": r.symbol,
                    "agent": r.agent_name,
                    "direction": r.direction,
                    "confidence": r.confidence,
                    "run_at": r.run_at,
                    "available": r.available,
                    "rationale": r.rationale,
                }
                for r in rows
            ],
            width="stretch",
        )
