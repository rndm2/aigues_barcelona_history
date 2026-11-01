"""Persist one bounded retry policy for provider session limits."""

from __future__ import annotations

import math
import time

from .const import (
    CONF_AUTH_SESSION_LIMIT,
    CONF_AUTH_SESSION_LIMIT_ATTEMPTS,
    CONF_RETRY_NOT_BEFORE,
)

SESSION_RETRY_INITIAL_SECONDS = 30 * 60
SESSION_RETRY_MAX_SECONDS = 6 * 60 * 60


def retry_delay(data, *, now=None) -> float:
    """Return the remaining durable pause, rejecting malformed deadlines."""
    deadline = data.get(CONF_RETRY_NOT_BEFORE, 0)
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
        raise ValueError("Invalid stored provider retry deadline")
    return max(0, deadline - (time.time() if now is None else now))


def session_retry_updates(data, error, *, now=None) -> dict:
    """Increase the cooldown only after an actual session-limit response."""
    now = time.time() if now is None else now
    previous = data.get(CONF_AUTH_SESSION_LIMIT_ATTEMPTS, 0)
    previous = previous if type(previous) is int and previous >= 0 else 0
    attempts = min(previous + 1, 5)
    delay = min(SESSION_RETRY_INITIAL_SECONDS * 2 ** (attempts - 1), SESSION_RETRY_MAX_SECONDS)
    requested = getattr(error, "retry_after", None)
    if (
        not isinstance(requested, bool)
        and isinstance(requested, (int, float))
        and math.isfinite(requested)
        and requested >= 0
    ):
        delay = max(delay, requested)
    return {
        CONF_AUTH_SESSION_LIMIT: True,
        CONF_AUTH_SESSION_LIMIT_ATTEMPTS: attempts,
        CONF_RETRY_NOT_BEFORE: now + max(delay, retry_delay(data, now=now)),
    }


def clear_session_retry(data) -> dict:
    """Clear session failures after success without shortening another pause."""
    if retry_delay(data):
        return {}
    return {
        key: value
        for key, value in (
            (CONF_AUTH_SESSION_LIMIT, False),
            (CONF_AUTH_SESSION_LIMIT_ATTEMPTS, 0),
        )
        if data.get(key)
    }
