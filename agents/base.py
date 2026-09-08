"""
Shared plumbing for all four analysis agents.

Each agent module (technical.py, fundamental.py, sentiment.py, macro.py)
only gathers its own slice of the store and writes a prompt — this file
handles calling Claude for the actual reasoning and persisting the
result to agent_signals, including marking an agent "unavailable" when
it has nothing to work with or the call fails, so the orchestrator can
redistribute its weight instead of silently treating it as neutral.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

import requests

from store.db import get_session
from store.models import AgentSignal

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-4-6")

RESPONSE_INSTRUCTIONS = """

Respond with ONLY a JSON object, no other text, in exactly this shape:
{
  "direction": "bullish" | "bearish" | "neutral",
  "confidence": <float 0.0-1.0>,
  "key_signals": ["...", "..."],
  "rationale": "1-3 sentence explanation"
}
"""


@dataclass
class AgentResult:
    direction: str
    confidence: float
    key_signals: List[str]
    rationale: str


def call_claude(system_prompt: str, data_summary: str) -> AgentResult:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    response = requests.post(
        ANTHROPIC_URL,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 500,
            "system": system_prompt + RESPONSE_INSTRUCTIONS,
            "messages": [{"role": "user", "content": data_summary}],
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    text = "".join(block["text"] for block in payload["content"] if block["type"] == "text")
    parsed = json.loads(text)

    return AgentResult(
        direction=parsed["direction"],
        confidence=float(parsed["confidence"]),
        key_signals=parsed.get("key_signals", []),
        rationale=parsed.get("rationale", ""),
    )


def persist_signal(symbol: str, agent_name: str, result: Optional[AgentResult], available: bool = True) -> None:
    with get_session() as session:
        session.add(
            AgentSignal(
                symbol=symbol,
                run_at=datetime.utcnow(),
                agent_name=agent_name,
                direction=result.direction if result else "neutral",
                confidence=result.confidence if result else 0.0,
                rationale=result.rationale if result else "agent unavailable this run",
                key_signals=result.key_signals if result else [],
                available=available,
            )
        )
