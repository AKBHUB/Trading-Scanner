"""
catalyst_confluence_scanner — scheduled consumer of the store, not a
live-fetch job (config/schedule.yaml: skills.catalyst_confluence_scanner).
Per ARCHITECTURE.md this runs at 9:45 AM / 2:45 PM CT against Tier 1
(news/sentiment), Tier 2 (insider/institutional — already fetched
pre-market), and Tier 4 (technical) snapshots.

CAVEAT: this drives orchestrator.compute_composite() — restricted to the
technical + sentiment agents (sentiment already combines Tier 1 news with
Tier 2 insider/congress/institutional data, see agents/sentiment.py) — as
its scoring engine, not the original SMC/momentum TVRemix funnel described
in the spec. That funnel's actual logic isn't present anywhere in this
codebase, and inventing one here would risk silently contradicting
whatever you already use elsewhere. If you have the real funnel, swap the
scoring call in run() for it; the candidate loop and store-reading
plumbing underneath don't need to change.
"""

from typing import Iterable

from agents import sentiment, technical
from orchestrator.composite import compute_composite

SKILL_NAME = "catalyst_confluence_scanner"
AGENTS = ["technical", "sentiment"]


def run(symbols: Iterable[str]) -> int:
    """
    For each candidate: refresh the technical (Tier 4) and sentiment
    (Tier 1 + Tier 2 combined) reads, then combine just those two via the
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
