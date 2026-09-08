"""
Shared HTTP client for Alpha Vantage, used by every ingestion tier.

Centralizing the client here means the shared rate limiter is the only
thing standing between any tier and your plan's per-minute quota — no
tier or agent calls the API directly, so nothing can accidentally burst
past the limit even when several jobs are due around the same time.
"""

import collections
import logging
import os
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://www.alphavantage.co/query"
API_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY")
RATE_LIMIT_PER_MINUTE = int(os.environ.get("ALPHA_VANTAGE_RATE_LIMIT_PER_MINUTE", "75"))

if not API_KEY:
    logger.warning("ALPHA_VANTAGE_API_KEY is not set — calls will fail.")


class AlphaVantageError(Exception):
    """Raised on an explicit error response, or after retries are exhausted on a throttle."""


class RateLimiter:
    """Sliding-window limiter: blocks the caller until a call fits under the per-minute cap."""

    def __init__(self, max_per_minute: int):
        self.max_per_minute = max_per_minute
        self._calls: collections.deque = collections.deque()

    def wait_for_slot(self) -> None:
        now = time.monotonic()
        window_start = now - 60
        while self._calls and self._calls[0] < window_start:
            self._calls.popleft()
        if len(self._calls) >= self.max_per_minute:
            sleep_for = 60 - (now - self._calls[0]) + 0.05
            if sleep_for > 0:
                logger.debug("Rate limit reached, sleeping %.2fs", sleep_for)
                time.sleep(sleep_for)
        self._calls.append(time.monotonic())


_rate_limiter = RateLimiter(RATE_LIMIT_PER_MINUTE)


def call(function: str, *, max_retries: int = 3, **params: Any) -> dict:
    """
    Call an Alpha Vantage `function` with the given params, respecting the
    shared rate limiter. Retries with backoff when Alpha Vantage responds
    with a "Note"/"Information" throttle message instead of a hard error.
    """
    query = {"function": function, "apikey": API_KEY, **params}

    for attempt in range(1, max_retries + 1):
        _rate_limiter.wait_for_slot()
        response = requests.get(BASE_URL, params=query, timeout=30)
        response.raise_for_status()
        data = response.json()

        if "Error Message" in data:
            raise AlphaVantageError(f"{function}: {data['Error Message']}")

        if "Note" in data or "Information" in data:
            msg = data.get("Note") or data.get("Information")
            logger.warning("%s throttled (attempt %d/%d): %s", function, attempt, max_retries, msg)
            time.sleep(2 ** attempt)
            continue

        return data

    raise AlphaVantageError(f"{function}: exhausted retries, still throttled")
