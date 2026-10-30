"""Pure manual provider-range intent and exact scoped reconciliation regressions."""

from copy import deepcopy
from datetime import date, datetime, timedelta
import json
import unittest

from test_history_energy_projection import daily_report, hourly_report
from test_history_statistics import MADRID, UTC, reading, statistics

TODAY = date(2026, 10, 30)
MAX_DAYS = 1825


def manual(start, end, *, today=TODAY, max_days=MAX_DAYS):
    return statistics.validate_hourly_import_range(
        start, end, today_provider=today, max_days=max_days
    )


def requested(start, end):
    return [{"start": start.isoformat(), "end": end.isoformat()}]


def pending(ranges, *, daily=None, hourly=None, today=TODAY, since=date(2026, 6, 12)):
    return statistics.pending_hourly_provider_days(
        daily or {},
        hourly or {},
        MADRID,
        hourly_since=since,
        current_provider_date=today,
        requested_ranges=ranges,
    )


def normalize(rows, start, end, *, provider_dates=True, **extra):
    return statistics.normalize_readings(
        rows,
        "HOURLY",
        MADRID,
        startdate=start,
        enddate=end,
        provider_dates=provider_dates,
        **extra,
    )


class ManualRangeValidationTests(unittest.TestCase):
    def test_inclusive_ui_dates_become_half_open_provider_dates(self):
        self.assertEqual(manual("2026-01-10", "2026-01-11"), (date(2026, 1, 10), date(2026, 1, 12)))
        self.assertEqual(manual(TODAY, TODAY), (TODAY, TODAY + timedelta(days=1)))
        self.assertEqual(manual("1901-01-01", "1901-01-01"), (date(1901, 1, 1), date(1901, 1, 2)))

    def test_inclusive_span_limit_is_exact_and_does_not_limit_history_age(self):
        first = TODAY - timedelta(days=MAX_DAYS - 1)
        self.assertEqual(manual(first, TODAY), (first, TODAY + timedelta(days=1)))
        with self.assertRaises(ValueError):
            manual(first - timedelta(days=1), TODAY)
        self.assertEqual(manual(date(1, 1, 1), date(1, 1, 2)), (date(1, 1, 1), date(1, 1, 3)))

    def test_noncanonical_wrong_types_datetimes_future_and_reversal_fail(self):
        for value in (
            None,
            True,
            1,
            {},
            [],
            "20260101",
            "2026-W01-1",
            "2026-1-01",
            "2026-01-01T00:00:00",
            datetime(2026, 1, 1),
            datetime(2026, 1, 1, tzinfo=UTC),
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    manual(value, "2026-01-01")
                with self.assertRaises(ValueError):
                    manual("2026-01-01", value)
        for start, end in (
            (TODAY + timedelta(days=1), TODAY + timedelta(days=1)),
            (TODAY, TODAY - timedelta(days=1)),
        ):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                manual(start, end)
        for today in ("2026-10-30", datetime(2026, 10, 30), None):
            with self.subTest(today=today), self.assertRaises(ValueError):
                manual(TODAY, TODAY, today=today)
        for limit in (None, True, 0, -1, 1.0, "1825"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                manual(TODAY, TODAY, max_days=limit)
        with self.assertRaises(ValueError):
            manual(date.max, date.max, today=date.max)


class RequestedRangeUnionTests(unittest.TestCase):
    def test_sort_overlap_adjacency_and_duplicate_union_is_idempotent_and_immutable(self):
        source = [
            {"start": "2026-01-05", "end": "2026-01-08"},
            {"start": "2026-01-01", "end": "2026-01-03"},
            {"start": "2026-01-02", "end": "2026-01-05"},
            {"start": "2026-01-10", "end": "2026-01-11"},
            {"start": "2026-01-01", "end": "2026-01-03"},
        ]
        before = deepcopy(source)
        expected = [
            {"start": "2026-01-01", "end": "2026-01-08"},
            {"start": "2026-01-10", "end": "2026-01-11"},
        ]
        actual = statistics.validate_hourly_requested_ranges(source)
        self.assertEqual(actual, expected)
        self.assertEqual(source, before)
        self.assertEqual(statistics.validate_hourly_requested_ranges(actual), expected)
        self.assertEqual(
            statistics.validate_hourly_requested_ranges(json.loads(json.dumps(actual))), expected
        )
        self.assertEqual(statistics.validate_hourly_requested_ranges(()), [])

    def test_add_shared_union_preserves_gap_and_accepts_unions_larger_than_one_request(self):
        first = date(2000, 1, 1)
        middle = first + timedelta(days=MAX_DAYS)
        end = middle + timedelta(days=MAX_DAYS)
        source = requested(first, middle)
        before = deepcopy(source)
        actual = statistics.add_hourly_requested_range(source, middle, end)
        self.assertEqual(actual, requested(first, end))
        self.assertEqual(source, before)
        self.assertEqual(
            statistics.add_hourly_requested_range(
                actual, end + timedelta(days=1), end + timedelta(days=2)
            ),
            actual + requested(end + timedelta(days=1), end + timedelta(days=2)),
        )

    def test_malformed_saved_metadata_and_add_arguments_fail_closed_without_mutation(self):
        for value in (
            None,
            False,
            {},
            "2026-01-01",
            [{"start": "2026-01-01"}],
            [{"start": "2026-01-01", "end": "2026-01-02", "other": 1}],
            [{"start": "20260101", "end": "2026-01-02"}],
            [{"start": date(2026, 1, 1), "end": "2026-01-02"}],
            [{"start": "2026-01-02", "end": "2026-01-02"}],
            [{"start": "2026-01-03", "end": "2026-01-02"}],
        ):
            before = deepcopy(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                statistics.validate_hourly_requested_ranges(value)
            self.assertEqual(value, before)
        for start, end in ((TODAY, TODAY), ("2026-01-01", TODAY), (datetime(2026, 1, 1), TODAY)):
            with self.subTest(start=start), self.assertRaises(ValueError):
                statistics.add_hourly_requested_range([], start, end)


class ExplicitPendingRangeTests(unittest.TestCase):
    def test_empty_old_request_retries_without_either_source_or_detail_frontier(self):
        first = date(1901, 1, 1)
        ranges = requested(first, first + timedelta(days=3))
        before = deepcopy(ranges)
        self.assertEqual(
            pending(ranges, since=None), [first + timedelta(days=offset) for offset in range(3)]
        )
        self.assertEqual(ranges, before)
        self.assertEqual(pending((), daily=daily_report(first, 0.1), since=None), [])

    def test_complete_days_leave_queue_while_partial_and_zero_days_stay_forever(self):
        first = date(2020, 1, 1)
        ranges = requested(first, first + timedelta(days=3))
        hours = hourly_report(first) | hourly_report(first + timedelta(days=1), hours=[0])
        before = deepcopy(hours)
        self.assertEqual(
            pending(ranges, hourly=hours), [first + timedelta(days=1), first + timedelta(days=2)]
        )
        self.assertEqual(hours, before)

    def test_current_intent_becomes_pending_only_when_its_provider_day_closes(self):
        ranges = requested(TODAY - timedelta(days=1), TODAY + timedelta(days=2))
        self.assertEqual(pending(ranges), [TODAY - timedelta(days=1)])
        self.assertEqual(
            pending(ranges, today=TODAY + timedelta(days=1)), [TODAY - timedelta(days=1), TODAY]
        )

    def test_dst_still_requires_24_provider_hours_and_explicit_scope_has_no_gap_leak(self):
        for day in (date(2026, 3, 29), date(2026, 10, 25)):
            with self.subTest(day=day):
                ranges = requested(day, day + timedelta(days=2)) + requested(
                    day + timedelta(days=3), day + timedelta(days=4)
                )
                self.assertEqual(
                    pending(ranges, hourly=hourly_report(day)),
                    [day + timedelta(days=1), day + timedelta(days=3)],
                )

    def test_full_requested_backlog_is_not_truncated_and_invalid_intent_rejected(self):
        first = date(1901, 1, 1)
        self.assertEqual(len(pending(requested(first, first + timedelta(days=40)))), 40)
        with self.assertRaises(ValueError):
            pending([{"start": "20260101", "end": "2026-01-02"}])


class ProviderRangeReconciliationTests(unittest.TestCase):
    def test_winter_scope_is_provider_dates_and_existing_ha_scope_is_unchanged(self):
        first, end = date(2026, 1, 10), date(2026, 1, 12)
        rows = [
            reading(f"2026-01-{day:02}T00:02:00+01:00", 100 + day, 0.1) for day in (9, 10, 11, 12)
        ]
        actual = normalize(rows, first, end)
        self.assertEqual(
            [row["observed_at"][:10] for row in actual.values()], ["2026-01-10", "2026-01-11"]
        )
        legacy = normalize(rows, first, end, provider_dates=False)
        self.assertEqual(
            [row["observed_at"][:10] for row in legacy.values()], ["2026-01-11", "2026-01-12"]
        )
        single = normalize(rows, first, end, provider_date=first)
        self.assertEqual(len(single), 1)
        self.assertEqual(next(iter(single.values()))["observed_at"][:10], "2026-01-10")

    def test_fixed_cet_scope_preserves_both_dst_folds_and_actual_offset(self):
        first = date(2026, 10, 25)
        rows = [
            reading("2026-10-25T02:02:00+02:00", 100, 0.1),
            reading("2026-10-25T02:02:00+01:00", 100.1, 0.1),
            reading("2026-10-26T00:02:00+02:00", 100.2, 0.1),
        ]
        actual = normalize(rows, first, first + timedelta(days=1))
        self.assertEqual(len(actual), 3)
        self.assertEqual(len(set(actual)), 3)

    def test_partial_corrections_keep_unreturned_hours_and_outside_source_unchanged(self):
        day = date(2026, 1, 10)
        current = (
            hourly_report(day - timedelta(days=1))
            | hourly_report(day)
            | hourly_report(day + timedelta(days=1))
        )
        rows = [
            reading("2026-01-10T00:01:00+01:00", 1000, 0.5),
            reading("2026-01-11T00:01:00+01:00", 1001, 0.6),
        ]
        incoming = statistics.normalize_readings(rows, "HOURLY", MADRID)
        before = deepcopy((current, incoming))
        actual = statistics.reconcile_intervals(
            current,
            incoming,
            "HOURLY",
            MADRID,
            startdate=day,
            enddate=day + timedelta(days=1),
            provider_dates=True,
        )
        changed = [key for key in actual if actual[key] != current[key]]
        self.assertEqual(changed, ["2026-01-09T22:00:00+00:00"])
        self.assertEqual(actual[changed[0]]["delta"], 0.5)
        self.assertEqual(len(actual), len(current))
        self.assertEqual((current, incoming), before)

    def test_filter_before_consolidation_for_raw_and_saved_outside_conflicts(self):
        day = date(2026, 1, 10)
        rows = [
            reading("2026-01-10T10:02:00+01:00", 100, 0.1),
            reading("2026-01-11T10:02:00+01:00", 101, 0.1),
            reading("2026-01-11T10:02:00+01:00", 999, 99),
        ]
        self.assertEqual(len(normalize(rows, day, day + timedelta(days=1))), 1)
        incoming = statistics.normalize_readings(rows[:2], "HOURLY", MADRID)
        outside_key = "2026-01-11T08:00:00+00:00"
        alias = outside_key.replace("+00:00", "Z")
        incoming[alias] = {**incoming[outside_key], "delta": 99}
        actual = statistics.reconcile_intervals(
            {},
            incoming,
            "HOURLY",
            MADRID,
            startdate=day,
            enddate=day + timedelta(days=1),
            provider_dates=True,
        )
        self.assertEqual(len(actual), 1)
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            statistics.validate_saved_ledger(incoming, "HOURLY", MADRID)

    def test_every_outside_row_is_validated_before_scope_and_bad_flags_fail_closed(self):
        day = date(2026, 1, 10)
        with self.assertRaises(ValueError):
            normalize([reading("2026-01-11T10:02:00+01:00", 100, -1)], day, day + timedelta(days=1))
        incoming = hourly_report(day + timedelta(days=1), hours=[0])
        key, row = next(iter(incoming.items()))
        row["observed_at"] = "2026-01-11T01:02:00+01:00"
        with self.assertRaises(ValueError):
            statistics.reconcile_intervals(
                {},
                incoming,
                "HOURLY",
                MADRID,
                startdate=day,
                enddate=day + timedelta(days=1),
                provider_dates=True,
            )
        for flag in (None, 1, "true"):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                normalize([], day, day, provider_dates=flag)
        with self.assertRaises(ValueError):
            statistics.normalize_readings([], "DAILY", MADRID, provider_dates=True)


if __name__ == "__main__":
    unittest.main()
