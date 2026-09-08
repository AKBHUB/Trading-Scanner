"""
swing_trade_scanner — scheduled consumer of the store, not a live-fetch
job (config/schedule.yaml: skills.swing_trade_scanner). Per ARCHITECTURE.md
this runs against Tier 4 (technical/core) data plus Tier 1 (news/sentiment)
for catalyst context.

CAVEAT: this drives orchestrator.compute_composite() — restricted to the
technical + sentiment agents — as its scoring engine, not the original
17-point technical rubric described in the spec. That rubric's actual
logic isn't present anywhere in this codebase, and inventing one here
would risk silently contradicting whatever you already use elsewhere. If
you have the real rubric, swap the scoring call in run() for it; the
candidate loop and store-reading plumbing underneath don't need to change.
"""

from typing import Iterable

from agents import sentiment, technical
from orchestrator.composite import compute_composite

SKILL_NAME = "swing_trade_scanner"
AGENTS = ["technical", "sentiment"]


def run(symbols: Iterable[str]) -> int:
    """
    For each candidate: refresh the technical (Tier 4) and sentiment
    (Tier 1 catalyst context) reads, then combine just those two via the
    composite orchestrator, persisted under this skill's name. Returns the
    number of symbols that produced a usable composite call.
    """
    scored = 0
    for symbol in symbols:
        technical.analyze(symbol)
        sentiment.analyze(symbol)
        result = compute_composite(symbol, skill_name=SKILL_NAME, agent_names=AGENTS)
        if result is not None:
            scored += 1
    return scored
