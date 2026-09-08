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

from agents import fundamental, macro, sentiment, technical
from ingestion import daily_reference, fundamentals, news_sentiment, technical_core
from store.config_store import get_config, set_config
from store.db import get_session, init_db
from store.models import AgentSignal, CongressTrade

init_db()

st.set_page_config(page_title="Trading-Scanner", layout="wide")
st.title("Trading-Scanner")

tab_config, tab_run, tab_signals = st.tabs(["Watchlist & Schedule", "Run Now", "Recent Signals"])

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
    daily_cfg = tiers_cfg.get("daily_reference", {}).get("jobs", {})

    col1, col2 = st.columns(2)
    with col1:
        news_interval = st.number_input(
            "News/sentiment interval (minutes)", min_value=5, value=int(news_cfg.get("interval_minutes", 30))
        )
        news_start = st.text_input("News window start (HH:MM)", value=news_cfg.get("start_time", "08:30"))
        news_end = st.text_input("News window end (HH:MM)", value=news_cfg.get("end_time", "15:15"))
    with col2:
        tech_interval = st.number_input(
            "Technical/core interval (minutes)", min_value=5, value=int(tech_cfg.get("interval_minutes", 15))
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
                "news_sentiment": {
                    "interval_minutes": news_interval,
                    "start_time": news_start,
                    "end_time": news_end,
                },
                "technical_core": {
                    "interval_minutes": tech_interval,
                    "market_hours_only": True,
                    "includes": ["ohlcv", "indicators", "index", "options"],
                },
                "daily_reference": {
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
    st.caption(f"Current watchlist: {', '.join(watchlist) or '(empty — set one in the Watchlist tab)'}")

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

    if c3.button("Run fundamentals watch"):
        with st.spinner("Checking earnings calendar and M&A news triggers..."):
            fundamentals.check_earnings_calendar_trigger(watchlist)
            fundamentals.check_news_ma_trigger(watchlist, since=datetime.utcnow() - timedelta(hours=1))
            count = fundamentals.refresh_flagged_symbols()
        st.success(f"Refreshed {count} flagged symbols.")

    if c4.button("Run technical & core"):
        with st.spinner("Fetching technical indicators, OHLCV, index, options..."):
            technical_core.run_technical(watchlist)
            technical_core.run_index_data(index_watchlist)
            technical_core.run_options(watchlist)
        st.success("Technical & core tier run complete.")

    st.divider()
    st.subheader("Agents")
    symbol = st.selectbox("Symbol", watchlist) if watchlist else st.text_input("Symbol")

    a1, a2, a3, a4 = st.columns(4)
    if symbol and a1.button("Run technical agent"):
        with st.spinner(f"Analyzing {symbol}..."):
            technical.analyze(symbol)
        st.success("Done — see Recent Signals tab.")
    if symbol and a2.button("Run fundamental agent"):
        with st.spinner(f"Analyzing {symbol}..."):
            fundamental.analyze(symbol)
        st.success("Done — see Recent Signals tab.")
    if symbol and a3.button("Run sentiment agent"):
        with st.spinner(f"Analyzing {symbol}..."):
            sentiment.analyze(symbol)
        st.success("Done — see Recent Signals tab.")
    if symbol and a4.button("Run macro agent"):
        with st.spinner(f"Analyzing {symbol}..."):
            macro.analyze(symbol, index_watchlist)
        st.success("Done — see Recent Signals tab.")

# ---------------------------------------------------------------------------
# Recent signals
# ---------------------------------------------------------------------------
with tab_signals:
    st.subheader("Recent agent signals")
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
            use_container_width=True,
        )
