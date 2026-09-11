"""Deterministic market sentiment and flow rubric for the sentiment agent."""

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class RubricResult:
    score: int
    maximum: int
    rows: list[dict]


def evaluate(news: Iterable, insider: Iterable, congress: Iterable, institutional: Iterable) -> RubricResult:
    news = list(news)
    insider = list(insider)
    congress = list(congress)
    institutional = list(institutional)

    news_score = _news_score(news)
    flow_score = _flow_score(insider, congress, institutional)
    macro_score = _macro_score(news)
    rows = [
        {
            "category": "Market Sentiment & Flow",
            "parameter": "News Sentiment Signal",
            "bullish": "Bullish sentiment catalyst / M&A / earnings beat",
            "bearish": "Bearish sentiment catalyst / litigation",
            "score": news_score,
        },
        {
            "category": "Market Sentiment & Flow",
            "parameter": "Insider & Institutional",
            "bullish": "Net insider buying / Congress trades accumulation",
            "bearish": "Heavy insider selling / Distribution",
            "score": flow_score,
        },
        {
            "category": "Market Sentiment & Flow",
            "parameter": "Macro & Topic Alignment",
            "bullish": "Favorable sector topic tailwinds",
            "bearish": "Adverse macro/fiscal/monetary headwinds",
            "score": macro_score,
        },
    ]
    return RubricResult(score=sum(row["score"] for row in rows), maximum=len(rows), rows=rows)


def _news_score(news: list) -> int:
    scores = [row.sentiment_score for row in news if row.sentiment_score is not None]
    headlines = " ".join((row.headline or "").lower() for row in news)
    bullish_terms = ("acquisition", "merger", "earnings beat", "raises guidance", "upgrade")
    bearish_terms = ("litigation", "lawsuit", "fraud", "downgrade", "misses estimates")
    if any(term in headlines for term in bearish_terms) and not any(term in headlines for term in bullish_terms):
        return -1
    if scores and sum(scores) / len(scores) >= 0.15:
        return 1
    if scores and sum(scores) / len(scores) <= -0.15:
        return -1
    return 0


def _flow_score(insider: list, congress: list, institutional: list) -> int:
    buying = sum(1 for row in insider + congress if _is_buy(row))
    selling = sum(1 for row in insider + congress if _is_sell(row))
    institutional_change = sum(row.change_pct or 0 for row in institutional)
    if selling > buying and selling >= 1 and institutional_change <= 0:
        return -1
    if buying > selling or institutional_change > 0:
        return 1
    return 0


def _macro_score(news: list) -> int:
    positive = 0.0
    negative = 0.0
    favorable_topics = {"technology", "finance", "energy_transportation", "manufacturing", "retail_wholesale"}
    adverse_topics = {"economy_fiscal", "economy_monetary", "economy_macro"}
    for row in news:
        value = row.sentiment_score or 0
        topics = set(row.topics or [])
        if topics & favorable_topics:
            positive += value
        if topics & adverse_topics:
            negative += value
    if negative <= -0.15:
        return -1
    if positive >= 0.15:
        return 1
    return 0


def _is_buy(row) -> bool:
    value = " ".join(str(getattr(row, field, "") or "") for field in ("transaction_type", "raw")).lower()
    return any(term in value for term in ("buy", "purchase", "acquisition", "acquire"))


def _is_sell(row) -> bool:
    value = " ".join(str(getattr(row, field, "") or "") for field in ("transaction_type", "raw")).lower()
    return any(term in value for term in ("sell", "sale", "disposal", "dispose"))