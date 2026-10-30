"""Normalize provider intervals without inventing consumption or missing hours.

DAILY timestamps identify provider calendar-day labels. Their supplied offset is
retained for audit, but converting the instant must not move a daily aggregate
into another labelled day. HOURLY timestamps identify meter readings at the end
of consumption intervals. HA statistic starts identify the preceding UTC hour.

Each frequency has its own source ledger. The derived Energy projection selects
one precision for each provider day, without changing or adding source readings.
"""

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timedelta, tzinfo
from datetime import timezone as datetime_timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
import math
import re

UTC = datetime_timezone.utc
HOURLY_INTERVAL = timedelta(hours=1)
PROVIDER_TIME_ZONE = datetime_timezone(timedelta(hours=1))
PROVIDER_DAY_HOURS = 24
ENERGY_COVERAGE_RECENT_DATES = 14
LEGACY_ARCHIVE_MIN_OVERLAP_DAYS = 3
_MISSING = object()


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


def _source_sha256(value):
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(serialized.encode()).hexdigest()


def _native_statistic_id(value):
    if not isinstance(value, str) or re.fullmatch(r"sensor\.[a-z0-9_]+", value) is None:
        raise ValueError("Archive provenance requires a native sensor statistic ID")
    return value


def _archive_origin(value):
    fields = {"type", "statistic_id", "start", "row_sha256", "previous_state"}
    if not isinstance(value, Mapping) or value.keys() != fields:
        raise ValueError("Archive origin must contain only the approved provenance fields")
    if value["type"] != "recorder_archive":
        raise ValueError("Unsupported ledger origin")
    _native_statistic_id(value["statistic_id"])
    _aware_datetime(value["start"], "origin.start")
    digest = value["row_sha256"]
    if not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
        raise ValueError("Archive origin requires a lowercase SHA256 row digest")
    return {
        **{key: value[key] for key in ("type", "statistic_id", "start", "row_sha256")},
        "previous_state": _number(value["previous_state"], "origin.previous_state"),
    }


def _record(start, observed_at, state, delta, *, origin=_MISSING):
    start = start.astimezone(UTC)
    if start.minute or start.second or start.microsecond:
        raise ValueError("Ledger starts must be aligned to a UTC hour")
    record = {
        "start": start.isoformat(),
        "observed_at": observed_at.isoformat(),
        "state": _number(state, "accumulatedConsumption"),
        "delta": _number(delta, "deltaConsumption"),
    }
    if origin is not _MISSING:
        record["origin"] = _archive_origin(origin)
        original_row = {
            "start": record["origin"]["start"],
            "state": record["state"],
            "sum": record["state"],
        }
        if record["origin"]["row_sha256"] != _source_sha256(original_row):
            raise ValueError("Archive source row digest does not match its meter reading")
        verified_delta = Decimal(str(record["state"])) - Decimal(
            str(record["origin"]["previous_state"])
        )
        if verified_delta < 0 or verified_delta != Decimal(str(record["delta"])):
            raise ValueError("Archived consumption must match its verified meter difference")
    return record


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
        record["state"] != current["state"]
        or record["delta"] != current["delta"]
        or record.get("origin") != current.get("origin")
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


def _provider_date(observed_at):
    """Use the provider's fixed-CET date for actual closing readings."""
    return observed_at.astimezone(PROVIDER_TIME_ZONE).date()


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
    return observed_at.astimezone(UTC).replace(minute=0, second=0, microsecond=0) - HOURLY_INTERVAL


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


def normalize_readings(
    raw, frequency, timezone: tzinfo, *, startdate=None, enddate=None, provider_date=None
):
    """Return a serialized ledger of validated, independently reported intervals.

    DAILY uses the provider's date label at midnight in the supplied business
    timezone. HOURLY preserves the supplied offset and assigns the consumption
    ending at a reading to the preceding UTC hour. A later observation replaces
    an older observation in the same interval. An identical observation with
    conflicting values is rejected.
    Optional bounds select a half-open range of HA dates for HOURLY, or
    provider calendar-date labels for the separate DAILY reference.
    An optional HOURLY provider date selects one complete fixed-CET closing
    block, including its actual intervals on the preceding HA date in winter.
    Every row is validated before filtering, while out-of-range rows cannot
    replace an in-range observation during bucket consolidation.
    """
    frequency = _frequency(frequency)
    if not isinstance(timezone, tzinfo):
        raise ValueError("A timezone is required")
    _validate_bounds(startdate, enddate, optional=True)
    _validate_bounds(provider_date, provider_date, optional=True)
    if provider_date is not None and frequency is not Frequency.HOURLY:
        raise ValueError("A provider-date filter is only valid for HOURLY readings")
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
        if provider_date is not None and _provider_date(observed_at) != provider_date:
            continue
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
                record = _record(
                    start,
                    observed_at,
                    value["state"],
                    value["delta"],
                    origin=value.get("origin", _MISSING),
                )
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
        if "origin" in record:
            native_day = (
                _aware_datetime(record["origin"]["start"], "origin.start")
                .astimezone(timezone)
                .date()
            )
            midnight = datetime.combine(observed_at.date(), time.min, tzinfo=timezone)
            if (
                frequency is not Frequency.DAILY
                or native_day != observed_at.date()
                or observed_at != midnight
            ):
                raise ValueError("Archived daily origin must match its HA date and midnight label")
        expected = canonical_start(observed_at, frequency, timezone)
        if _aware_datetime(record["start"], "start") != expected:
            raise ValueError(
                f"Stored {frequency.value} interval start does not match its observation"
            )
    return canonical


def migrate_legacy_hourly_ledger(ledger, timezone: tzinfo):
    """Rekey validated reading-time buckets as consumption-interval starts.

    Legacy data floored the reading instant, placing its preceding consumption
    one actual hour late. The UTC translation is bijective through DST changes.
    Original observations, readings and deltas remain unchanged. Reject a mixed
    or damaged ledger rather than applying another shift to corrected records.
    """
    canonical = merge_ledgers(ledger)
    migrated = {}
    for record in canonical.values():
        observed_at = _aware_datetime(record["observed_at"], "observed_at")
        start = canonical_start(observed_at, Frequency.HOURLY, timezone)
        if _aware_datetime(record["start"], "start") != start + HOURLY_INTERVAL:
            raise ValueError("Stored legacy HOURLY start does not match its reading time")
        updated = {**record, "start": start.isoformat()}
        _insert(migrated, updated)
    return validate_saved_ledger(migrated, Frequency.HOURLY, timezone)


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


def build_legacy_daily_prefix(native_rows, daily, timezone, *, statistic_id):
    """Archive only verified native daily increments preceding API history.

    This pure, one-time migration accepts explicitly supplied recorder rows.
    It never reads the original integration or its database. The first native
    value is an unknown lifetime baseline and is never imported as consumption.
    Every following HA calendar date must be unique, contiguous and monotonic,
    with state equal to sum. At least three consecutive overlap days must match
    actual API daily states and reported deltas before any prefix is returned.

    Archives retain the preceding meter state, native timestamp and a digest of
    canonical validated JSON containing its start, state and sum. Their daily
    observation is local midnight, allowing later API observations to supersede
    the archived record. The summary is private migration evidence, not attrs.
    """
    statistic_id = _native_statistic_id(statistic_id)
    known = validate_saved_ledger(daily, Frequency.DAILY, timezone)
    api = {
        _aware_datetime(row["observed_at"], "observed_at").date(): row
        for row in known.values()
        if "origin" not in row
    }
    if not api:
        raise ValueError("Actual API daily history is required to validate the archive bridge")
    first_api_day = min(api)
    if not isinstance(native_rows, Iterable) or isinstance(native_rows, (str, bytes, Mapping)):
        raise ValueError("Native rows must be an iterable of mappings")
    rows = []
    for value in native_rows:
        if not isinstance(value, Mapping):
            raise ValueError("Each native row must be a mapping")
        try:
            observed = _aware_datetime(value["start"], "native.start")
            row = {
                "start": value["start"],
                "state": _number(value["state"], "native.state"),
                "sum": _number(value["sum"], "native.sum"),
            }
        except KeyError as error:
            raise ValueError(f"Native row is missing {error.args[0]}") from error
        if row["state"] != row["sum"]:
            raise ValueError("Native state and sum must match before archive import")
        rows.append((observed.astimezone(timezone).date(), row))
    rows.sort(key=lambda item: item[0])
    if len(rows) < LEGACY_ARCHIVE_MIN_OVERLAP_DAYS + 1:
        raise ValueError("Insufficient native rows for an independent API overlap")

    prefix = {}
    matches = []
    total = Decimal(0)
    for (previous_day, previous), (day, row) in zip(rows, rows[1:]):
        if day != previous_day + timedelta(days=1):
            raise ValueError("Native calendar dates must be unique and contiguous")
        delta = Decimal(str(row["state"])) - Decimal(str(previous["state"]))
        if delta < 0:
            raise ValueError("Native meter reset or negative consumption cannot be archived")
        if day in api:
            reference = api[day]
            if Decimal(str(row["state"])) != Decimal(str(reference["state"])) or delta != Decimal(
                str(reference["delta"])
            ):
                raise ValueError("Native/API overlap state or consumption does not match")
            matches.append(day)
        if day >= first_api_day:
            continue
        observation = datetime.combine(day, time.min, tzinfo=timezone)
        point = _record(
            canonical_start(observation, Frequency.DAILY, timezone),
            observation,
            row["state"],
            delta,
            origin={
                "type": "recorder_archive",
                "statistic_id": statistic_id,
                "start": row["start"],
                "row_sha256": _source_sha256(row),
                "previous_state": previous["state"],
            },
        )
        _insert(prefix, point)
        total += delta
    overlap_span = LEGACY_ARCHIVE_MIN_OVERLAP_DAYS - 1
    if not any(
        matches[index] - matches[index - overlap_span] == timedelta(days=overlap_span)
        for index in range(overlap_span, len(matches))
    ):
        raise ValueError("At least three consecutive matching API overlap days are required")
    baseline_day, baseline = rows[0]
    if baseline_day in api and Decimal(str(baseline["state"])) != Decimal(
        str(api[baseline_day]["state"])
    ):
        raise ValueError("Native/API baseline overlap state does not match")
    prefix = validate_saved_ledger(prefix, Frequency.DAILY, timezone)
    imported_days = sorted(
        _aware_datetime(row["observed_at"], "observed_at").date().isoformat()
        for row in prefix.values()
    )
    summary = {
        "statistic_id": statistic_id,
        "native_rows": len(rows),
        "source_sha256": _source_sha256([row for _, row in rows]),
        "baseline": {
            "day": baseline_day.isoformat(),
            **baseline,
            "row_sha256": _source_sha256(baseline),
            "consumption_imported": False,
        },
        "first_api_day": first_api_day.isoformat(),
        "matching_overlap_days": len(matches),
        "overlap_dates": [day.isoformat() for day in matches],
        "imported_days": len(prefix),
        "first_imported_day": imported_days[0] if imported_days else None,
        "last_imported_day": imported_days[-1] if imported_days else None,
        "imported_consumption_m3": _number(total, "archived consumption"),
    }
    return prefix, summary


def _daily_provider_reports(daily, timezone):
    """Daily observations retain their provider date label rather than an instant date."""
    return {
        _aware_datetime(record["observed_at"], "observed_at").date(): record
        for record in validate_saved_ledger(daily, Frequency.DAILY, timezone).values()
    }


def _provider_hourly_blocks(hourly, timezone):
    """Partition actual intervals by the fixed-CET date of their closing hour."""
    blocks = defaultdict(dict)
    for key, record in validate_saved_ledger(hourly, Frequency.HOURLY, timezone).items():
        start = _aware_datetime(record["start"], "start")
        provider_day = _provider_date(start + HOURLY_INTERVAL)
        blocks[provider_day][key] = record
    return blocks


def _provider_block_last_start(day, timezone):
    """Anchor a coarse provider day inside its own actual 24-hour block.

    End-reading labels 00 through 23 in fixed CET close intervals starting at
    23 on the previous provider date through 22 on this date. In Madrid the
    final interval starts at 23 in summer or 22 in winter. It is also one of
    the actual hourly keys when that provider block becomes complete.
    """
    closing_reading = datetime.combine(day, time(hour=23), tzinfo=PROVIDER_TIME_ZONE)
    return canonical_start(closing_reading, Frequency.HOURLY, timezone)


def initial_energy_hourly_since(hourly, timezone):
    """Choose an initial provider-date frontier; the caller persists it once.

    Prefer the earliest complete provider block so an initial partial day can
    use its whole-day report. A later backfill must not recompute this setting.
    Complete older islands are selected independently by the projection.
    """
    blocks = _provider_hourly_blocks(hourly, timezone)
    complete = [day for day, rows in blocks.items() if len(rows) == PROVIDER_DAY_HOURS]
    return min(complete or blocks, default=None)


def pending_hourly_provider_days(daily, hourly, timezone, *, hourly_since, current_provider_date):
    """Return every closed provider block still needing reported hours.

    A known partial hourly block remains eligible at any age, including before
    the Energy detail frontier. Daily-known dates without hours are eligible
    only at or after that frontier. Ancient intentionally coarse daily history
    is therefore not expanded into an unbounded initial download. This list is
    independent of the Energy fallback policy and is never truncated for attrs.
    """
    _validate_bounds(hourly_since, hourly_since, optional=True)
    _validate_bounds(current_provider_date, current_provider_date)
    reports = _daily_provider_reports(daily, timezone)
    blocks = _provider_hourly_blocks(hourly, timezone)
    return [
        day
        for day in sorted(reports.keys() | blocks.keys())
        if day < current_provider_date
        and len(blocks.get(day, {})) < PROVIDER_DAY_HOURS
        and (day in blocks or (hourly_since is not None and day >= hourly_since))
    ]


def build_energy_projection(
    daily,
    hourly,
    timezone,
    *,
    hourly_since,
    use_daily_totals_for_incomplete=True,
    current_provider_date=None,
):
    """Select actual hours or one explicitly coarse total per provider block.

    The two inputs remain authoritative, independent source ledgers. A complete
    block always uses its actual hours, even before the persisted frontier.
    Otherwise a daily report can replace the entire incomplete block, never
    coexist with its partial hours or distribute a residual across invented
    hours. Current and future provider blocks always retain only actual hours.
    With the optional policy disabled, recent incomplete blocks wait for their
    reported hours, even when none have arrived yet. Only dates before the fixed
    frontier (or without a frontier) can then use past daily reports. A daily
    amount below known hourly consumption is contradictory, so actual hours win
    and the disagreement is exposed. Without a current provider date all supplied
    dates are treated as closed; the caller supplies the date without clock I/O.

    Coarse dates describe provider reporting periods, not measured hourly use.
    Their physical block boundaries remain fixed CET through Madrid DST. This
    prevents overlap at a winter coarse/fine handoff. The returned projection
    is a derived ledger for ``build_statistics``, not a new HOURLY source ledger.
    """
    _validate_bounds(hourly_since, hourly_since, optional=True)
    _validate_bounds(current_provider_date, current_provider_date, optional=True)
    if not isinstance(use_daily_totals_for_incomplete, bool):
        raise ValueError("The incomplete-day policy must be a boolean")
    daily_by_day = _daily_provider_reports(daily, timezone)
    blocks = _provider_hourly_blocks(hourly, timezone)
    projection = {}
    coverage = {
        "hourly_since": hourly_since.isoformat() if hourly_since is not None else None,
        "use_daily_totals_for_incomplete": use_daily_totals_for_incomplete,
        "current_provider_date": (
            current_provider_date.isoformat() if current_provider_date is not None else None
        ),
        "daily_aggregate_dates": [],
        "reported_hours": 0,
        "suppressed_reported_hours": 0,
        "complete_hourly_blocks": 0,
        "incomplete_provider_dates": {},
        "disagreement_dates": [],
        "pending_provider_dates": [],
        "updating_provider_day": None,
        "updating_reported_hours": 0,
        "updating_provider_day_complete": False,
    }
    for day in sorted(daily_by_day.keys() | blocks.keys()):
        hours = blocks.get(day, {})
        complete = len(hours) == PROVIDER_DAY_HOURS
        coverage["complete_hourly_blocks"] += int(complete)
        closed = current_provider_date is None or day < current_provider_date
        if day == current_provider_date:
            coverage["updating_provider_day"] = day.isoformat()
            coverage["updating_reported_hours"] = len(hours)
            coverage["updating_provider_day_complete"] = complete
        if (
            closed
            and not complete
            and not use_daily_totals_for_incomplete
            and hourly_since is not None
            and day >= hourly_since
        ):
            coverage["pending_provider_dates"].append(day.isoformat())
        report = daily_by_day.get(day)
        known_amount = sum((Decimal(str(row["delta"])) for row in hours.values()), Decimal(0))
        daily_amount = Decimal(str(report["delta"])) if report is not None else None
        contradicts_known = daily_amount is not None and daily_amount < known_amount
        if contradicts_known or (complete and daily_amount not in (None, known_amount)):
            coverage["disagreement_dates"].append(day.isoformat())
        use_coarse = (
            report is not None
            and not complete
            and not contradicts_known
            and closed
            and (use_daily_totals_for_incomplete or hourly_since is None or day < hourly_since)
        )
        if hours and not complete:
            coverage["incomplete_provider_dates"][day.isoformat()] = {
                "known": len(hours),
                "expected": PROVIDER_DAY_HOURS,
            }
        if use_coarse:
            point = _record(
                _provider_block_last_start(day, timezone),
                _aware_datetime(report["observed_at"], "observed_at"),
                report["state"],
                report["delta"],
                origin=report.get("origin", _MISSING),
            )
            _insert(projection, point)
            coverage["daily_aggregate_dates"].append(day.isoformat())
            coverage["suppressed_reported_hours"] += len(hours)
        else:
            projection.update(hours)
            coverage["reported_hours"] += len(hours)
    aggregate_dates = coverage["daily_aggregate_dates"]
    coverage["daily_aggregate_days"] = len(aggregate_dates)
    coverage["first_daily_aggregate_date"] = aggregate_dates[0] if aggregate_dates else None
    coverage["last_daily_aggregate_date"] = aggregate_dates[-1] if aggregate_dates else None
    coverage["incomplete_provider_days"] = len(coverage["incomplete_provider_dates"])
    coverage["disagreement_days"] = len(coverage["disagreement_dates"])
    coverage["pending_provider_days"] = len(coverage["pending_provider_dates"])
    for key in ("daily_aggregate_dates", "disagreement_dates", "pending_provider_dates"):
        coverage[key] = coverage[key][-ENERGY_COVERAGE_RECENT_DATES:]
    coverage["incomplete_provider_dates"] = dict(
        list(coverage["incomplete_provider_dates"].items())[-ENERGY_COVERAGE_RECENT_DATES:]
    )
    return projection, coverage


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
