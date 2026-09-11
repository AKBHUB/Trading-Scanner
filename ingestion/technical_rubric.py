"""Deterministic Tier 4 rubric for technical, momentum, and options flow."""

from typing import Any


def evaluate(ohlcv: dict, indicators: dict, options: dict | None = None) -> dict:
    technical = _technical_score(indicators)
    momentum = _momentum_score(indicators)
    options_score = _options_score(options or {})
    rows = [
        {
            "category": "Tier 4 Tech & Options",
            "parameter": "17-pt Technical Rubric",
            "bullish": "17-pt technical trend & momentum bullish",
            "bearish": "17-pt technical trend bearish",
            "score": technical,
        },
        {
            "category": "Tier 4 Tech & Options",
            "parameter": "Intraday Momentum / SMC",
            "bullish": "SMC market structure break & bullish momentum",
            "bearish": "SMC market structure break bearish",
            "score": momentum,
        },
        {
            "category": "Tier 4 Tech & Options",
            "parameter": "Options Flow & Volatility",
            "bullish": "Unusual call flow / Low IV skew",
            "bearish": "Unusual put flow / High IV skew",
            "score": options_score,
        },
    ]
    return {"score": sum(row["score"] for row in rows), "maximum": 3, "rows": rows}


def _technical_score(indicators: dict) -> int:
    rsi = _latest(indicators.get("RSI"), "RSI")
    adx = _latest(indicators.get("ADX"), "ADX")
    macd_hist = _latest(indicators.get("MACD"), "MACD_Hist")
    bullish = int(rsi is not None and 50 <= rsi <= 70) + int(adx is not None and adx >= 20) + int(macd_hist is not None and macd_hist > 0)
    bearish = int(rsi is not None and rsi < 40) + int(adx is not None and adx >= 20 and macd_hist is not None and macd_hist < 0)
    return 1 if bullish > bearish else -1 if bearish > bullish else 0


def _momentum_score(indicators: dict) -> int:
    macd_hist = _latest(indicators.get("MACD"), "MACD_Hist")
    obv = _latest(indicators.get("OBV"), "OBV")
    if macd_hist is not None and macd_hist > 0 and (obv is None or obv > 0):
        return 1
    if macd_hist is not None and macd_hist < 0 and (obv is None or obv < 0):
        return -1
    return 0


def _options_score(options: dict) -> int:
    calls = puts = 0.0
    for row in options.get("data", options.get("option_chain", [])) if isinstance(options, dict) else []:
        contract = str(row).lower()
        volume = _number(row, "volume") or 0
        if "call" in contract:
            calls += volume
        elif "put" in contract:
            puts += volume
    if calls > puts * 1.25 and calls > 0:
        return 1
    if puts > calls * 1.25 and puts > 0:
        return -1
    return 0


def _latest(payload: Any, key: str) -> float | None:
    if not isinstance(payload, dict):
        return None
    target = payload.get("Technical Analysis: " + key)
    if target is not None:
        return _first_number(target, key)
    return _first_number(payload, key)


def _first_number(payload: Any, key: str) -> float | None:
    if isinstance(payload, dict):
        direct = _number(payload, key)
        if direct is not None:
            return direct
        for value in payload.values():
            result = _first_number(value, key)
            if result is not None:
                return result
    return None


def _number(payload: Any, key: str) -> float | None:
    if not isinstance(payload, dict):
        return None
    for candidate in (key, key.lower(), "value", "4. close", "volume"):
        try:
            if payload.get(candidate) is not None:
                return float(payload[candidate])
        except (TypeError, ValueError):
            return None
    return None