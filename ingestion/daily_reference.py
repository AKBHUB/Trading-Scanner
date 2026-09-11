"""
Tier 2 — daily one-time reference data: insider transactions, congress
trades, politician metadata, institutional holdings. Each has its own
scheduled time (config/schedule.yaml: tiers.daily_reference.jobs) but
shares the same "fetch, write to store" shape.
"""

from datetime import datetime
import re
from typing import Iterable

from ingestion.alpha_vantage_client import call
from store.config_store import get_config
from store.db import get_session
from store.models import (
    CongressTrade,
    InsiderTransaction,
    InstitutionalHolding,
    PoliticianMetadata,
)


def run_insider_transactions(symbols: Iterable[str]) -> int:
    written = 0
    minimum_value = _minimum("min_insider_transaction_value")
    with get_session() as session:
        for symbol in symbols:
            data = call("INSIDER_TRANSACTIONS", symbol=symbol)
            for row in data.get("data", []):
                shares = _safe_float(row.get("shares"))
                price = _safe_float(row.get("share_price"))
                transaction_value = abs(shares * price) if shares is not None and price is not None else None
                if transaction_value is None or transaction_value < minimum_value:
                    continue
                session.add(
                    InsiderTransaction(
                        symbol=symbol,
                        transaction_date=_parse_date(row.get("transaction_date")),
                        insider_name=row.get("executive"),
                        title=row.get("executive_title"),
                        transaction_type=row.get("acquisition_or_disposal"),
                        shares=shares,
                        price=price,
                        raw=row,
                    )
                )
                written += 1
    return written


def run_congress_trades(symbols: Iterable[str]) -> int:
    written = 0
    minimum_value = _minimum("min_congress_transaction_value")
    with get_session() as session:
        for symbol in symbols:
            data = call("CONGRESS_TRADES", symbol=symbol)
            for row in data.get("data", []):
                amount_range = row.get("amount")
                if _amount_range_lower_bound(amount_range) < minimum_value:
                    continue
                session.add(
                    CongressTrade(
                        symbol=symbol,
                        transaction_date=_parse_date(row.get("transaction_date")),
                        bioguide_id=row.get("bioguide_id"),
                        chamber=row.get("chamber"),
                        transaction_type=row.get("transaction_type"),
                        amount_range=amount_range,
                        raw=row,
                    )
                )
                written += 1
    return written


def run_politician_metadata(bioguide_ids: Iterable[str], only_missing: bool = True) -> int:
    """
    Politician metadata is near-static reference data. With
    only_missing=True (the default, and what config/schedule.yaml assumes),
    this skips any bioguide_id already in the store — the daily job runs
    every day, but only does real work for politicians not seen before.
    """
    written = 0
    with get_session() as session:
        existing_ids = set()
        if only_missing:
            existing_ids = {row.bioguide_id for row in session.query(PoliticianMetadata.bioguide_id).all()}

        for bioguide_id in bioguide_ids:
            if only_missing and bioguide_id in existing_ids:
                continue
            data = call("POLITICIAN_METADATA", bioguide_id=bioguide_id)
            info = data.get("data") or data
            session.merge(
                PoliticianMetadata(
                    bioguide_id=bioguide_id,
                    name=info.get("name"),
                    chamber=info.get("chamber"),
                    party=info.get("party"),
                    state=info.get("state"),
                    raw=info,
                )
            )
            written += 1
    return written


def run_institutional_holdings(symbols: Iterable[str]) -> int:
    written = 0
    minimum_value = _minimum("min_institutional_holding_value")
    with get_session() as session:
        for symbol in symbols:
            data = call("INSTITUTIONAL_HOLDINGS", symbol=symbol)
            for row in data.get("data", []):
                value = _safe_float(row.get("value"))
                if value is None or abs(value) < minimum_value:
                    continue
                session.add(
                    InstitutionalHolding(
                        symbol=symbol,
                        report_date=_parse_date(row.get("report_date")),
                        institution_name=row.get("institution_name") or row.get("filer_name"),
                        shares_held=_safe_float(row.get("shares_held")),
                        value=value,
                        change_pct=_safe_float(row.get("change_percent")),
                        raw=row,
                    )
                )
                written += 1
    return written


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _minimum(key: str) -> float:
    filters = get_config("transaction_filters", {})
    try:
        return max(float(filters.get(key, 0)), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _amount_range_lower_bound(value) -> float:
    """Return the conservative lower bound from a congressional amount range."""
    if isinstance(value, (int, float)):
        return float(value)
    values = [float(number.replace(",", "")) for number in re.findall(r"\$?([\d,]+(?:\.\d+)?)", str(value or ""))]
    return values[0] if values else 0.0
