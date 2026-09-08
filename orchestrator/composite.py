"""
Orchestrator — combines the four agents' latest stored signals for a
symbol into one weighted composite call, per ARCHITECTURE.md:

    composite = Σ (weight_i × confidence_i × direction_i)   direction_i ∈ {-1, 0, +1}

Reads agent_signals (written by agents/*.py) rather than running the
agents itself — same store-as-boundary shape as everything else here.
An agent with no signal on record for the symbol, or whose latest
signal was marked unavailable, has its weight redistributed
proportionally across the agents that do have one, rather than being
silently treated as neutral (which would understate the real signal
instead of just excluding it).
"""

from typing import Dict, Optional

from store.db import get_session
from store.models import AgentSignal, FinalSignal

AGENT_NAMES = ["technical", "fundamental", "sentiment", "macro"]

# Equal weighting until there's enough outcome_correct history in
# final_signals to tune these against real accuracy per agent, per
# ARCHITECTURE.md's "Orchestrator" section.
DEFAULT_WEIGHTS = {name: 0.25 for name in AGENT_NAMES}

DIRECTION_SIGN = {"bullish": 1, "bearish": -1, "neutral": 0}

# Matches the neutral band Alpha Vantage itself uses for ticker sentiment
# scores (see NewsSentiment's sentiment_score_definition) for consistency
# with how the rest of the app labels a score as neutral vs. directional.
BULLISH_THRESHOLD = 0.15
BEARISH_THRESHOLD = -0.15


def _latest_signals(symbol: str) -> Dict[str, AgentSignal]:
    """Latest AgentSignal row per agent name for `symbol`. An agent that
    has never run for this symbol is simply absent from the result."""
    latest: Dict[str, AgentSignal] = {}
    with get_session() as session:
        for agent_name in AGENT_NAMES:
            row = (
                session.query(AgentSignal)
                .filter_by(symbol=symbol, agent_name=agent_name)
                .order_by(AgentSignal.run_at.desc())
                .first()
            )
            if row is not None:
                latest[agent_name] = row
    return latest


def compute_composite(symbol: str, skill_name: str = "composite_orchestrator") -> Optional[FinalSignal]:
    """
    Reads each agent's latest stored signal for `symbol`, redistributes
    weight away from any agent that's missing or unavailable, computes the
    weighted composite, and persists + returns the resulting FinalSignal.

    Returns None if no agent has a usable signal on record — nothing to
    combine yet (run the agents for this symbol first).
    """
    signals = _latest_signals(symbol)
    usable = {
        name: row
        for name, row in signals.items()
        if row.available and row.direction in DIRECTION_SIGN
    }

    if not usable:
        return None

    base_weight_total = sum(DEFAULT_WEIGHTS[name] for name in usable)
    weights = {name: DEFAULT_WEIGHTS[name] / base_weight_total for name in usable}

    composite_score = sum(
        weights[name] * row.confidence * DIRECTION_SIGN[row.direction] for name, row in usable.items()
    )

    if composite_score > BULLISH_THRESHOLD:
        direction = "bullish"
    elif composite_score < BEARISH_THRESHOLD:
        direction = "bearish"
    else:
        direction = "neutral"

    final = FinalSignal(
        symbol=symbol,
        skill_name=skill_name,
        direction=direction,
        confidence=min(abs(composite_score), 1.0),
        composite_score=composite_score,
        contributing_agents=weights,
    )

    with get_session() as session:
        session.add(final)
        # expire_on_commit=False (store/db.py) means `final`'s attributes,
        # including the id/run_at populated at flush, stay readable after
        # this block commits and closes.

    return final
