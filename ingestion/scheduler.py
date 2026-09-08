"""
Entry point: wires the four ingestion tiers to APScheduler using the
cadence defined in config/schedule.yaml.

Run as a long-lived process:
    python -m ingestion.scheduler
"""

import logging
from datetime import datetime, timedelta

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ingestion import daily_reference, fundamentals, news_sentiment, technical_core
from store.db import get_session, init_db
from store.models import CongressTrade

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

with open("config/schedule.yaml") as f:
    CONFIG = yaml.safe_load(f)

# TODO: replace with your actual scan universe — ideally shared with
# swing-trade-scanner / catalyst-confluence-scanner's candidate lists
# rather than a separate hardcoded list.
WATCHLIST = ["AAPL", "MSFT", "NVDA"]
INDEX_WATCHLIST = ["SPX", "NDX"]


def job_news_sentiment():
    window = CONFIG["tiers"]["news_sentiment"]
    if not news_sentiment.within_window(window["start_time"], window["end_time"]):
        return
    count = news_sentiment.run(WATCHLIST)
    logger.info("news_sentiment: wrote %d rows", count)


def job_insider_transactions():
    count = daily_reference.run_insider_transactions(WATCHLIST)
    logger.info("insider_transactions: wrote %d rows", count)


def job_congress_trades():
    count = daily_reference.run_congress_trades(WATCHLIST)
    logger.info("congress_trades: wrote %d rows", count)


def job_politician_metadata():
    with get_session() as session:
        bioguide_ids = {
            row.bioguide_id
            for row in session.query(CongressTrade.bioguide_id).distinct()
            if row.bioguide_id
        }
    count = daily_reference.run_politician_metadata(bioguide_ids)
    logger.info("politician_metadata: wrote %d rows", count)


def job_institutional_holdings():
    count = daily_reference.run_institutional_holdings(WATCHLIST)
    logger.info("institutional_holdings: wrote %d rows", count)


def job_fundamentals_watch():
    fundamentals.check_earnings_calendar_trigger(WATCHLIST)
    fundamentals.check_news_ma_trigger(WATCHLIST, since=datetime.utcnow() - timedelta(hours=1))
    # new_8k_filing isn't wired up yet — that trigger comes from TradingView,
    # not Alpha Vantage. Add a third check_* call here once that connector exists.
    count = fundamentals.refresh_flagged_symbols()
    if count:
        logger.info("fundamentals: refreshed %d flagged symbols", count)


def job_technical_core():
    technical_core.run_technical(WATCHLIST)
    technical_core.run_index_data(INDEX_WATCHLIST)
    technical_core.run_options(WATCHLIST)
    logger.info("technical_core: tier run complete")


def _cron_at(hhmm: str) -> CronTrigger:
    hour, minute = hhmm.split(":")
    return CronTrigger(hour=int(hour), minute=int(minute))


def main():
    init_db()
    scheduler = BlockingScheduler(timezone=CONFIG["timezone"])

    news_cfg = CONFIG["tiers"]["news_sentiment"]
    scheduler.add_job(job_news_sentiment, IntervalTrigger(minutes=news_cfg["interval_minutes"]))

    daily_cfg = CONFIG["tiers"]["daily_reference"]["jobs"]
    scheduler.add_job(job_insider_transactions, _cron_at(daily_cfg["insider_transactions"]))
    scheduler.add_job(job_congress_trades, _cron_at(daily_cfg["congress_trades"]))
    scheduler.add_job(job_institutional_holdings, _cron_at(daily_cfg["institutional_holdings"]))
    scheduler.add_job(job_politician_metadata, _cron_at(daily_cfg["politician_metadata"]))

    # Fundamentals watcher runs on the same cadence as news_sentiment —
    # it's cheap since the M&A check reads the store, not the API.
    scheduler.add_job(job_fundamentals_watch, IntervalTrigger(minutes=news_cfg["interval_minutes"]))

    tech_cfg = CONFIG["tiers"]["technical_core"]
    scheduler.add_job(job_technical_core, IntervalTrigger(minutes=tech_cfg["interval_minutes"]))

    logger.info("Scheduler starting...")
    scheduler.start()


if __name__ == "__main__":
    main()
