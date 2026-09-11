"""
Technical agent — reads the freshest TechnicalSnapshot for a symbol
(written by ingestion/technical_core.py) and judges direction from the
OHLCV bars and indicator values. Never calls Alpha Vantage itself.
"""

from agents.base import call_claude, persist_signal
from store.db import get_session
from store.models import TechnicalSnapshot

SYSTEM_PROMPT = """
You are a technical analysis agent for swing trading. You're given the
latest OHLCV bars and indicator values (RSI, MACD, EMA/SMA, Bollinger
Bands, OBV, ADX) for one symbol. Judge whether the technical picture
favors higher or lower prices over the next few days.

Use the supplied Tier 4 rubric as a structured prior: technical trend,
intraday momentum/SMC, and options flow/volatility.
"""


def analyze(symbol: str) -> None:
    with get_session() as session:
        snapshot = (
            session.query(TechnicalSnapshot)
            .filter_by(symbol=symbol)
            .order_by(TechnicalSnapshot.fetched_at.desc())
            .first()
        )

    if snapshot is None:
        persist_signal(symbol, "technical", None, available=False)
        return

    summary = (
        f"Symbol: {symbol}\n"
        f"Timeframe: {snapshot.timeframe}\n"
        f"OHLCV: {snapshot.ohlcv}\n"
        f"Indicators: {snapshot.indicators}\n"
        f"Tier 4 rubric: {(snapshot.indicators or {}).get('technical_rubric')}"
    )

    try:
        result = call_claude(SYSTEM_PROMPT, summary)
        persist_signal(symbol, "technical", result)
    except Exception as e:
        persist_signal(symbol, "technical", None, available=False, error=f"{type(e).__name__}: {e}")
