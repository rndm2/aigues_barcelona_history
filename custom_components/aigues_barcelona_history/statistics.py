"""Normalize provider intervals without inventing consumption or missing hours.

DAILY timestamps identify provider calendar-day labels. Their supplied offset is
retained for audit, but converting the instant must not move a daily aggregate
into another labelled day. HOURLY timestamps identify actual measurement times.

Each frequency has its own ledger. Mixing DAILY and HOURLY ledgers would count
the same consumption twice and is the caller's responsibility to prevent.
"""

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timedelta, tzinfo
from datetime import timezone as datetime_timezone
from decimal import Decimal
from enum import Enum
import math

UTC = datetime_timezone.utc


class Frequency(str, Enum):
    """Provider interval frequencies."""

    DAILY = "DAILY"
    HOURLY = "HOURLY"


def _frequency(value):
    try:
        return Frequency(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Unsupported consumption frequency: {value!r}") from error


def _aware_datetime(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp with an offset")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"Invalid {field}: {value!r}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include an offset")
    return parsed


def _number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{field} must be a finite non-negative number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{field} must be a finite non-negative number") from error
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field} must be a finite non-negative number")
    return result


def _record(start, observed_at, state, delta):
    start = start.astimezone(UTC)
    if start.minute or start.second or start.microsecond:
        raise ValueError("Ledger starts must be aligned to a UTC hour")
    return {
        "start": start.isoformat(),
        "observed_at": observed_at.isoformat(),
        "state": _number(state, "accumulatedConsumption"),
        "delta": _number(delta, "deltaConsumption"),
    }


def _insert(ledger, record, *, allow_correction=False):
    """Reject ambiguous response duplicates, but allow later response corrections."""
    key = record["start"]
    current = ledger.get(key)
    if current is None:
        ledger[key] = record
        return
    previous_time = _aware_datetime(current["observed_at"], "observed_at")
    incoming_time = _aware_datetime(record["observed_at"], "observed_at")
    if incoming_time > previous_time or (allow_correction and incoming_time == previous_time):
        ledger[key] = record
    elif incoming_time == previous_time and (
        record["state"] != current["state"] or record["delta"] != current["delta"]
    ):
        raise ValueError(f"Conflicting readings for the same observation at {key}")


def _validate_bounds(startdate, enddate, *, optional=False):
    for value in (startdate, enddate):
        if optional and value is None:
            continue
        if not isinstance(value, date) or isinstance(value, datetime):
            raise ValueError("Window bounds must be calendar dates")
    if startdate is not None and enddate is not None and startdate > enddate:
        raise ValueError("Window end precedes its start")


def _start_of_local_day(day: date, timezone: tzinfo) -> datetime:
    """Return one shared local-midnight boundary as an actual UTC instant."""
    return datetime.combine(day, time.min, tzinfo=timezone).astimezone(UTC)


def canonical_start(observed_at: datetime, frequency, timezone: tzinfo):
    """Use one interval rule for provider responses and persisted source data."""
    frequency = _frequency(frequency)
    if not isinstance(timezone, tzinfo):
        raise ValueError("A timezone is required")
    if (
        not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
    ):
        raise ValueError("Observation must be a datetime with an offset")
    if frequency is Frequency.DAILY:
        return _start_of_local_day(observed_at.date(), timezone)
    return observed_at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _in_window(observed_at, frequency, timezone, startdate, enddate):
    """Use HA calendar dates for hours and provider labels for daily references."""
    interval_date = (
        observed_at.date()
        if frequency is Frequency.DAILY
        else canonical_start(observed_at, frequency, timezone).astimezone(timezone).date()
    )
    return (startdate is None or interval_date >= startdate) and (
        enddate is None or interval_date < enddate
    )


def normalize_readings(raw, frequency, timezone: tzinfo, *, startdate=None, enddate=None):
    """Return a serialized ledger of validated, independently reported intervals.

    DAILY uses the provider's date label at midnight in the supplied business
    timezone. HOURLY preserves the supplied offset and floors the actual UTC
    instant. A later observation replaces an older observation in the same
    interval. An identical observation with conflicting values is rejected.
    Optional bounds select a half-open range of HA dates for HOURLY, or
    provider calendar-date labels for the separate DAILY reference.
    Every row is validated before filtering, while out-of-range rows cannot
    replace an in-range observation during bucket consolidation.
    """
    frequency = _frequency(frequency)
    if not isinstance(timezone, tzinfo):
        raise ValueError("A timezone is required")
    _validate_bounds(startdate, enddate, optional=True)
    if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes, Mapping)):
        raise ValueError("Readings must be an iterable of mappings")
    ledger = {}
    for metric in raw:
        if not isinstance(metric, Mapping):
            raise ValueError("Each reading must be a mapping")
        try:
            observed_at = _aware_datetime(metric["datetime"], "datetime")
            state = metric["accumulatedConsumption"]
            delta = metric["deltaConsumption"]
        except KeyError as error:
            raise ValueError(f"Reading is missing {error.args[0]}") from error
        record = _record(
            canonical_start(observed_at, frequency, timezone),
            observed_at,
            state,
            delta,
        )
        if not _in_window(observed_at, frequency, timezone, startdate, enddate):
            continue
        _insert(ledger, record)
    return ledger


def merge_ledgers(*ledgers):
    """Merge same-frequency response snapshots without mutating their inputs.

    The caller keeps DAILY and HOURLY in separate stores. Every entry is
    validated again because ledgers may have been loaded from persistent JSON.
    A later snapshot may correct values at an unchanged observation timestamp.
    Conflicting duplicate entries inside one snapshot remain ambiguous errors.
    """
    merged = {}
    for ledger in ledgers:
        if not isinstance(ledger, Mapping):
            raise ValueError("Each ledger must be a mapping")
        snapshot = {}
        for key, value in ledger.items():
            if not isinstance(value, Mapping):
                raise ValueError("Each ledger entry must be a mapping")
            try:
                start = _aware_datetime(value["start"], "start")
                observed_at = _aware_datetime(value["observed_at"], "observed_at")
                record = _record(start, observed_at, value["state"], value["delta"])
            except KeyError as error:
                raise ValueError(f"Ledger entry is missing {error.args[0]}") from error
            if _aware_datetime(key, "ledger key").astimezone(UTC) != start:
                raise ValueError("Ledger key does not match its interval start")
            _insert(snapshot, record)
        for record in snapshot.values():
            _insert(merged, record, allow_correction=True)
    return merged


def validate_saved_ledger(ledger, frequency, timezone: tzinfo):
    """Reject valid-looking stored records assigned to another interval or series."""
    frequency = _frequency(frequency)
    if not isinstance(timezone, tzinfo):
        raise ValueError("A timezone is required")
    canonical = merge_ledgers(ledger)
    for record in canonical.values():
        observed_at = _aware_datetime(record["observed_at"], "observed_at")
        expected = canonical_start(observed_at, frequency, timezone)
        if _aware_datetime(record["start"], "start") != expected:
            raise ValueError(
                f"Stored {frequency.value} interval start does not match its observation"
            )
    return canonical


def reconcile_intervals(current, incoming, frequency, timezone, *, startdate, enddate):
    """Apply only returned intervals inside the requested calendar range.

    A later fetch is authoritative for an existing HOURLY interval, even if
    its observation minute moved backwards. DAILY references retain their
    latest observation. Missing response intervals never erase durable data.
    Later cumulative sums are rebuilt separately.
    """
    frequency = _frequency(frequency)
    _validate_bounds(startdate, enddate)
    result = validate_saved_ledger(current, frequency, timezone)
    updates = validate_saved_ledger(incoming, frequency, timezone)
    selected = {}
    for key, record in updates.items():
        observed_at = _aware_datetime(record["observed_at"], "observed_at")
        if _in_window(observed_at, frequency, timezone, startdate, enddate):
            selected[key] = record
    if frequency is Frequency.DAILY:
        return merge_ledgers(result, selected)
    return result | selected


def _interval_starts(ledger):
    return sorted(_aware_datetime(row["start"], "start") for row in ledger.values())


def _interval_span(starts):
    return {
        "first_interval": starts[0].isoformat() if starts else None,
        "last_interval": starts[-1].isoformat() if starts else None,
        "known_intervals": len(starts),
    }


def describe_interval_coverage(ledger) -> dict:
    """Describe the actual saved span without filling absent source intervals."""
    return _interval_span(_interval_starts(ledger))


def describe_hourly_coverage(ledger, timezone: tzinfo) -> dict:
    """Describe provider-reported hourly coverage without creating source rows.

    The covered span includes whole local calendar days from the earliest to the
    latest known UTC bucket. First and last days can therefore be incomplete.
    Missing dates within that span are included, while dates after the latest
    known date are outside it. Only incomplete dates are listed individually.
    """
    canonical = validate_saved_ledger(ledger, Frequency.HOURLY, timezone)
    starts = _interval_starts(canonical)
    coverage = {
        **_interval_span(starts),
        "expected_intervals": 0,
        "complete_days": 0,
        "incomplete_days": {},
    }
    if not starts:
        return coverage

    known_per_day = Counter(start.astimezone(timezone).date() for start in starts)
    day = starts[0].astimezone(timezone).date()
    last_day = starts[-1].astimezone(timezone).date()
    while day <= last_day:
        beginning = _start_of_local_day(day, timezone)
        ending = _start_of_local_day(day + timedelta(days=1), timezone)
        # Count aligned UTC bucket starts belonging to this local date. The
        # ceiling also handles business zones whose offset includes minutes.
        first_hour = beginning.replace(minute=0, second=0, microsecond=0)
        if first_hour < beginning:
            first_hour += timedelta(hours=1)
        expected = math.ceil((ending - first_hour).total_seconds() / 3600)
        known = known_per_day[day]
        coverage["expected_intervals"] += expected
        if known == expected:
            coverage["complete_days"] += 1
        else:
            coverage["incomplete_days"][day.isoformat()] = {
                "known": known,
                "expected": expected,
            }
        day += timedelta(days=1)
    return coverage


def build_statistics(ledger):
    """Build deterministic HA points from the complete known-interval ledger.

    Sum begins with the first provider delta, not the physical meter's lifetime
    reading. Missing intervals remain absent and contribute no invented amount.
    Rebuilding the complete ledger also updates later sums after a correction or
    backfill. The physical meter reading remains available in ``state``.
    """
    canonical = merge_ledgers(ledger)
    cumulative = Decimal(0)
    points = []
    for key in sorted(canonical):
        record = canonical[key]
        cumulative += Decimal(str(record["delta"]))
        points.append(
            {
                "start": _aware_datetime(record["start"], "start"),
                "state": record["state"],
                "sum": _number(cumulative, "cumulative consumption"),
            }
        )
    return points


def plan_windows(startdate: date, enddate: date, frequency, maxdays=7, *, split_at=None):
    """Plan exact API requests for a half-open range of provider calendar dates.

    DAILY's API end date is inclusive. HOURLY's API end date is exclusive.
    Splitting never rounds the request to a Monday or includes unwanted dates.
    An optional split keeps today's request separate from historical queries.
    """
    frequency = _frequency(frequency)
    _validate_bounds(startdate, enddate)
    if split_at is not None:
        _validate_bounds(split_at, split_at)
    if isinstance(maxdays, bool) or not isinstance(maxdays, int) or maxdays < 1:
        raise ValueError("maxdays must be a positive integer")
    windows = []
    cursor = startdate
    while cursor < enddate:
        stop = min(cursor + timedelta(days=maxdays), enddate)
        if split_at is not None and cursor < split_at < stop:
            stop = split_at
        api_end = stop - timedelta(days=1) if frequency is Frequency.DAILY else stop
        windows.append((cursor, api_end))
        cursor = stop
    return windows
