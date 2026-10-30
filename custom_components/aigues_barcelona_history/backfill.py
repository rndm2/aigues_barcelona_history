"""Pure request-budget planning for delayed historical hourly reports."""

from collections.abc import Iterable, Mapping
from datetime import date, datetime
import math

MAX_HOURLY_BACKFILL_REQUESTS = 2
REGULAR_HOURLY_LOOKBACK_DAYS = 7
SECONDS_PER_DAY = 86400


def _calendar_date(value, field):
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValueError(f"{field} must be a calendar date")
    return value


def _timestamp(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite non-negative Unix timestamp")
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{field} must be a finite non-negative Unix timestamp") from error
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field} must be a finite non-negative Unix timestamp")
    return result


def validate_hourly_backfill_attempts(attempts):
    """Validate persisted scheduling metadata without changing it."""
    if not isinstance(attempts, Mapping):
        raise ValueError("Hourly backfill attempts must be a mapping")
    validated = {}
    for key, value in attempts.items():
        if not isinstance(key, str):
            raise ValueError("Backfill attempt keys must be canonical ISO dates")
        try:
            day = date.fromisoformat(key)
        except ValueError as error:
            raise ValueError("Backfill attempt keys must be canonical ISO dates") from error
        if day.isoformat() != key:
            raise ValueError("Backfill attempt keys must be canonical ISO dates")
        validated[key] = _timestamp(value, "Backfill attempt")
    return validated


def _retry_interval(age):
    if age <= REGULAR_HOURLY_LOOKBACK_DAYS:
        return None
    if age <= 30:
        return SECONDS_PER_DAY
    if age <= 180:
        return 7 * SECONDS_PER_DAY
    return 30 * SECONDS_PER_DAY


def plan_hourly_backfill(
    pending_days,
    attempts,
    *,
    now,
    current_provider_date,
    max_requests=MAX_HOURLY_BACKFILL_REQUESTS,
):
    """Select a bounded fair retry plan without reading a clock or marking attempts.

    The regular poll already retries the most recent seven provider dates.
    Additional requests retry age 8-30 days daily, 31-180 days weekly, and older
    dates every 30 days without an expiry. Dates with the oldest actual attempt
    run first; never-attempted dates tie by provider date. Future attempt times
    are treated as unattempted so clock rollback cannot indefinitely starve a
    date. The caller records only requests that actually performed provider I/O.
    """
    now = _timestamp(now, "Current time")
    today = _calendar_date(current_provider_date, "Current provider date")
    if (
        isinstance(max_requests, bool)
        or not isinstance(max_requests, int)
        or not 0 <= max_requests <= MAX_HOURLY_BACKFILL_REQUESTS
    ):
        raise ValueError("Hourly backfill request budget must be between zero and two")
    if not isinstance(pending_days, Iterable) or isinstance(pending_days, (str, bytes, Mapping)):
        raise ValueError("Pending provider dates must be an iterable of calendar dates")
    days = {_calendar_date(day, "Pending provider date") for day in pending_days}
    attempts = validate_hourly_backfill_attempts(attempts)
    due = []
    for day in days:
        interval = _retry_interval((today - day).days)
        if interval is None:
            continue
        last_attempt = attempts.get(day.isoformat())
        if last_attempt is None or last_attempt > now:
            due.append((-1, day))
        elif now - last_attempt >= interval:
            due.append((last_attempt, day))
    return [day for _, day in sorted(due)[:max_requests]]
