"""Pure delayed-hour scope and persistent retry-budget regressions."""

from copy import deepcopy
from datetime import date, datetime, timedelta
import importlib.util
from pathlib import Path
import unittest

from test_history_energy_projection import daily_report, hourly_report
from test_history_statistics import MADRID, UTC, reading, statistics

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "custom_components/aigues_barcelona_history/backfill.py"
)
SPEC = importlib.util.spec_from_file_location("history_backfill", MODULE_PATH)
backfill = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backfill)
TODAY = date(2026, 10, 30)
NOW = datetime(2026, 10, 30, tzinfo=UTC).timestamp()
DAY_SECONDS = 86400


def pending(daily=None, hourly=None, *, since=date(2026, 6, 12), today=TODAY):
    return statistics.pending_hourly_provider_days(
        daily or {}, hourly or {}, MADRID, hourly_since=since, current_provider_date=today
    )


def plan(days, attempts=None, *, now=NOW, today=TODAY, budget=2):
    return backfill.plan_hourly_backfill(
        days, attempts or {}, now=now, current_provider_date=today, max_requests=budget
    )


class PendingProviderDayTests(unittest.TestCase):
    def test_known_partial_before_frontier_and_recent_zero_day_are_pending(self):
        partial_day = date(2026, 6, 11)
        zero_day = date(2026, 6, 13)
        daily = (
            daily_report(date(2023, 10, 26), 0.1)
            | daily_report(partial_day, 0.1)
            | daily_report(zero_day, 0.1)
            | daily_report(TODAY, 0.1)
            | daily_report(TODAY + timedelta(days=1), 0.1)
        )
        hours = hourly_report(partial_day, hours=range(7, 24)) | hourly_report(date(2026, 6, 12))
        before = deepcopy((daily, hours))
        self.assertEqual(pending(daily, hours), [partial_day, zero_day])
        self.assertEqual((daily, hours), before)

    def test_no_age_expiry_for_known_partial_block_and_no_ancient_zero_hour_expansion(self):
        old = date(2020, 1, 10)
        hours = hourly_report(old, hours=[0])
        daily = daily_report(old - timedelta(days=1), 0.1)
        self.assertEqual(pending(daily, hours), [old])
        self.assertEqual(pending(daily, hours, since=None), [old])
        self.assertEqual(pending(daily, since=None), [])

    def test_full_pending_list_is_not_the_fourteen_date_diagnostics_sample(self):
        first = date(2026, 7, 1)
        days = [first + timedelta(days=index) for index in range(40)]
        daily = {}
        for day in days:
            daily |= daily_report(day, 0.1)
        self.assertEqual(pending(daily), days)

    def test_complete_provider_blocks_are_independent_of_23_and_25_hour_civil_days(self):
        for day in (date(2026, 3, 29), date(2026, 10, 25)):
            with self.subTest(day=day):
                complete = hourly_report(day)
                partial_next = hourly_report(day + timedelta(days=1), hours=[0])
                self.assertEqual(
                    pending(
                        hourly=complete | partial_next, since=day, today=day + timedelta(days=2)
                    ),
                    [day + timedelta(days=1)],
                )
                self.assertEqual(
                    pending(hourly=complete, since=day, today=day + timedelta(days=1)), []
                )

    def test_current_future_and_invalid_metadata_cannot_be_queued(self):
        hours = hourly_report(TODAY, hours=[0]) | hourly_report(
            TODAY + timedelta(days=1), hours=[0]
        )
        self.assertEqual(pending(hourly=hours), [])
        for today in (None, "2026-10-30", datetime(2026, 10, 30)):
            with self.subTest(today=today), self.assertRaises(ValueError):
                pending(today=today)
        with self.assertRaises(ValueError):
            pending(since="2026-06-12")
        malformed = hourly_report(date(2026, 6, 11), hours=[0])
        next(iter(malformed.values()))["delta"] = -1
        with self.assertRaises(ValueError):
            pending(hourly=malformed)


class ProviderScopedNormalizationTests(unittest.TestCase):
    def test_winter_provider_scope_retains_the_actual_hour_on_previous_ha_date(self):
        day = date(2026, 1, 10)
        source = [
            reading("2026-01-09T23:02:00+01:00", 100, 0.1),
            reading("2026-01-10T00:02:00+01:00", 100.1, 0.1),
            reading("2026-01-10T23:02:00+01:00", 100.2, 0.1),
            reading("2026-01-11T00:02:00+01:00", 100.3, 0.1),
        ]
        actual = statistics.normalize_readings(source, "HOURLY", MADRID, provider_date=day)
        self.assertEqual(
            list(actual),
            ["2026-01-09T22:00:00+00:00", "2026-01-10T21:00:00+00:00"],
        )
        self.assertEqual(len(actual), 2)

    def test_provider_date_uses_the_actual_fixed_cet_instant_not_original_iso_date(self):
        source = [
            reading("2026-06-11T00:02:00+02:00", 100, 0.1),
            reading("2026-06-12T00:02:00+02:00", 100.1, 0.1),
        ]
        actual = statistics.normalize_readings(
            source, "HOURLY", MADRID, provider_date=date(2026, 6, 11)
        )
        self.assertEqual(len(actual), 1)
        self.assertEqual(next(iter(actual.values()))["observed_at"], "2026-06-12T00:02:00+02:00")

    def test_outside_duplicate_conflicts_cannot_shadow_or_reject_an_in_scope_reading(self):
        source = [
            reading("2026-01-10T10:02:00+01:00", 100, 0.1),
            reading("2026-01-11T10:02:00+01:00", 100.1, 0.1),
            reading("2026-01-11T10:02:00+01:00", 999, 99),
        ]
        actual = statistics.normalize_readings(
            source, "HOURLY", MADRID, provider_date=date(2026, 1, 10)
        )
        self.assertEqual(len(actual), 1)
        self.assertEqual(next(iter(actual.values()))["delta"], 0.1)

    def test_every_outside_row_is_still_validated_before_filtering(self):
        source = [reading("2026-01-11T10:02:00+01:00", 100, -1)]
        before = deepcopy(source)
        with self.assertRaises(ValueError):
            statistics.normalize_readings(source, "HOURLY", MADRID, provider_date=date(2026, 1, 10))
        self.assertEqual(source, before)

    def test_same_scope_conflict_and_invalid_frequency_or_provider_date_fail_closed(self):
        duplicate = [
            reading("2026-01-10T10:02:00+01:00", 100, 0.1),
            reading("2026-01-10T10:02:00+01:00", 100, 0.2),
        ]
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            statistics.normalize_readings(
                duplicate, "HOURLY", MADRID, provider_date=date(2026, 1, 10)
            )
        for frequency, provider_day in (
            ("DAILY", date(2026, 1, 10)),
            ("HOURLY", "2026-01-10"),
            ("HOURLY", datetime(2026, 1, 10)),
        ):
            with self.subTest(frequency=frequency, provider_day=provider_day):
                with self.assertRaises(ValueError):
                    statistics.normalize_readings([], frequency, MADRID, provider_date=provider_day)


class HourlyBackfillPlannerTests(unittest.TestCase):
    def test_retry_cadences_have_exact_boundaries_and_no_oldest_age(self):
        for age, cadence in ((8, 1), (30, 1), (31, 7), (180, 7), (181, 30), (2000, 30)):
            with self.subTest(age=age):
                day = TODAY - timedelta(days=age)
                before_due = NOW - cadence * DAY_SECONDS + 1
                due = NOW - cadence * DAY_SECONDS
                self.assertEqual(plan([day], {day.isoformat(): before_due}), [])
                self.assertEqual(plan([day], {day.isoformat(): due}), [day])
        self.assertEqual(plan([date(1901, 1, 1)]), [date(1901, 1, 1)])

    def test_recent_dates_are_left_to_normal_polling_and_current_future_are_excluded(self):
        days = [
            TODAY + timedelta(days=1),
            TODAY,
            TODAY - timedelta(days=1),
            TODAY - timedelta(days=7),
        ]
        self.assertEqual(plan(days), [])

    def test_budget_and_round_robin_prevent_one_empty_day_starving_other_dates(self):
        days = [TODAY - timedelta(days=age) for age in (10, 40, 100, 300, 400)]
        remaining = sorted(days)
        attempts = {}
        for expected in (remaining[:2], remaining[2:4], remaining[4:]):
            selected = plan(reversed(days), attempts)
            self.assertEqual(selected, expected)
            self.assertLessEqual(len(selected), 2)
            # Simulate the integration recording only actual provider I/O.
            attempts.update({day.isoformat(): NOW for day in selected})
        self.assertEqual(plan(days, attempts), [])

    def test_oldest_last_attempt_wins_before_calendar_age(self):
        days = [TODAY - timedelta(days=age) for age in (10, 40, 300)]
        attempts = {
            days[0].isoformat(): NOW - 2 * DAY_SECONDS,
            days[1].isoformat(): NOW - 8 * DAY_SECONDS,
            days[2].isoformat(): NOW - 31 * DAY_SECONDS,
        }
        self.assertEqual(plan(days, attempts), [days[2], days[1]])

    def test_clock_rollback_does_not_pause_a_day_until_future_attempt_timestamp(self):
        day = TODAY - timedelta(days=365)
        self.assertEqual(plan([day], {day.isoformat(): NOW + 1000 * DAY_SECONDS}), [day])
        self.assertEqual(plan([day], {day.isoformat(): NOW}), [])

    def test_duplicate_pending_dates_do_not_consume_two_requests_or_mutate_attempts(self):
        day = TODAY - timedelta(days=8)
        attempts = {day.isoformat(): NOW - DAY_SECONDS}
        before = deepcopy(attempts)
        self.assertEqual(plan([day, day], attempts), [day])
        self.assertEqual(attempts, before)
        self.assertEqual(plan([day], attempts, budget=0), [])
        self.assertEqual(plan([day], attempts, budget=1), [day])

    def test_attempt_schema_rejects_noncanonical_dates_and_invalid_timestamps(self):
        for attempts in (None, [], False, "2026-01-01"):
            with self.subTest(attempts_type=type(attempts).__name__), self.assertRaises(ValueError):
                backfill.validate_hourly_backfill_attempts(attempts)
        for key in (None, date(2026, 1, 1), "20260101", "2026-W01-1", "2026-13-01"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                backfill.validate_hourly_backfill_attempts({key: NOW})
        for value in (None, True, "1", -1, float("nan"), float("inf"), 1 << 2000):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                backfill.validate_hourly_backfill_attempts({"2026-01-01": value})
        self.assertEqual(
            backfill.validate_hourly_backfill_attempts({"2026-01-01": 0}), {"2026-01-01": 0.0}
        )

    def test_planner_rejects_bad_schema_budget_and_clock_without_io(self):
        for budget in (True, -1, 3, 1.0):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                plan([], budget=budget)
        for now in (None, True, -1, float("nan"), float("inf"), 1 << 2000):
            with self.subTest(now_type=type(now).__name__), self.assertRaises(ValueError):
                plan([], now=now)
        for days in (None, {}, "2026-01-01", ["2026-01-01"], [datetime(2026, 1, 1)]):
            with self.subTest(days_type=type(days).__name__), self.assertRaises(ValueError):
                plan(days)
        with self.assertRaises(ValueError):
            plan([], today=datetime(2026, 10, 30))


if __name__ == "__main__":
    unittest.main()
