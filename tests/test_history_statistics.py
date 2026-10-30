"""Pure interval-ledger tests, requiring no HA runtime or network access."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

# Load the pure module without executing the integration's HA-dependent init.
MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "custom_components/aigues_barcelona_history/statistics.py"
)
SPEC = importlib.util.spec_from_file_location("history_statistics", MODULE_PATH)
statistics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(statistics)
MADRID = ZoneInfo("Europe/Madrid")
UTC = timezone.utc


def reading(timestamp, state, delta):
    return {
        "datetime": timestamp,
        "accumulatedConsumption": state,
        "deltaConsumption": delta,
    }


def normalize(values, frequency="HOURLY"):
    return statistics.normalize_readings(values, frequency, MADRID)


class LedgerTests(unittest.TestCase):
    def test_first_daily_sum_is_reported_consumption_not_lifetime_meter(self):
        ledger = normalize([reading("2025-10-27T23:01:00+01:00", 163.28, 0.123)], "DAILY")
        points = statistics.build_statistics(ledger)
        self.assertEqual(points[0]["state"], 163.28)
        self.assertEqual(points[0]["sum"], 0.123)

    def test_sparse_hourly_data_does_not_assign_the_meter_jump_to_one_hour(self):
        ledger = normalize(
            [
                reading("2026-10-27T01:01:00+01:00", 654.306, 0.002),
                reading("2026-10-28T02:01:00+01:00", 655.796, 0.010),
            ]
        )
        points = statistics.build_statistics(ledger)
        self.assertEqual(len(points), 2)
        self.assertEqual([point["sum"] for point in points], [0.002, 0.012])
        self.assertAlmostEqual(points[1]["sum"] - points[0]["sum"], 0.010)
        self.assertEqual(
            [point["start"] for point in points],
            [datetime(2026, 10, 26, 23, tzinfo=UTC), datetime(2026, 10, 28, 0, tzinfo=UTC)],
        )

    def test_backfill_and_correction_rebuild_later_sums_without_repeat_drift(self):
        initial = normalize(
            [
                reading("2026-10-28T10:01:00Z", 100.1, 0.1),
                reading("2026-10-28T12:01:00Z", 100.5, 0.3),
            ]
        )
        update = normalize(
            [
                reading("2026-10-28T11:01:00Z", 100.2, 0.1),
                reading("2026-10-28T12:02:00Z", 100.6, 0.4),
            ]
        )
        merged = statistics.merge_ledgers(initial, update)
        repeated = statistics.merge_ledgers(merged, update)
        self.assertEqual(merged, repeated)
        self.assertEqual(
            [point["sum"] for point in statistics.build_statistics(merged)],
            [0.1, 0.2, 0.6],
        )
        self.assertEqual(len(initial), 2)
        self.assertEqual(len(update), 2)

    def test_decimal_accumulation_avoids_float_addition_drift(self):
        ledger = normalize(
            [
                reading("2026-10-28T10:01:00Z", Decimal("100.1"), Decimal("0.1")),
                reading("2026-10-28T11:01:00Z", Decimal("100.3"), Decimal("0.2")),
            ]
        )
        self.assertEqual(statistics.build_statistics(ledger)[-1]["sum"], 0.3)

    def test_zero_is_preserved_and_daily_partial_updates_replace_earlier_observations(self):
        initial = normalize([reading("2026-10-28T10:01:00+01:00", 0, 0)], "DAILY")
        update = normalize([reading("2026-10-28T17:01:00+01:00", 0.756, 0.756)], "DAILY")
        self.assertEqual(statistics.build_statistics(initial)[0]["sum"], 0.0)
        result = statistics.build_statistics(statistics.merge_ledgers(initial, update))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["sum"], 0.756)

    def test_latest_observation_wins_independently_of_response_order_or_offset(self):
        older = reading("2026-10-28T11:01:00+01:00", 10.0, 0.5)
        later = reading("2026-10-28T10:02:00Z", 9.9, 0.4)
        first = normalize([later, older])
        second = normalize([older, later])
        self.assertEqual(first, second)
        self.assertEqual(next(iter(first.values()))["state"], 9.9)
        self.assertEqual(next(iter(first.values()))["observed_at"], "2026-10-28T10:02:00+00:00")

    def test_equal_timestamp_refetch_corrects_values_and_rebuilds_later_sums(self):
        first = normalize([reading("2026-10-28T11:01:00+01:00", 10.0, 0.5)])
        equivalent = normalize([reading("2026-10-28T10:01:00Z", 10.0, 0.5)])
        identical = statistics.merge_ledgers(first, equivalent)
        self.assertEqual(statistics.build_statistics(identical), statistics.build_statistics(first))
        tail = normalize([reading("2026-10-28T11:01:00Z", 10.2, 0.2)])
        correction = normalize([reading("2026-10-28T10:01:00Z", 10.1, 0.6)])
        merged = statistics.merge_ledgers(first, tail, correction)
        self.assertEqual(
            [point["sum"] for point in statistics.build_statistics(merged)],
            [0.6, 0.8],
        )
        self.assertEqual(statistics.build_statistics(first)[0]["sum"], 0.5)

    def test_conflicting_duplicates_within_one_response_are_rejected(self):
        values = [
            reading("2026-10-28T10:01:00Z", 10.0, 0.5),
            reading("2026-10-28T10:01:00Z", 10.1, 0.5),
        ]
        with self.assertRaises(ValueError):
            normalize(values)

    def test_conflicting_aliased_keys_inside_one_snapshot_are_rejected(self):
        ledger = normalize([reading("2026-10-28T10:01:00Z", 10.0, 0.5)])
        key, record = next(iter(ledger.items()))
        ledger[key.replace("+00:00", "Z")] = {**record, "delta": 0.6}
        with self.assertRaises(ValueError):
            statistics.merge_ledgers(ledger)

    def test_repeated_fall_back_hour_remains_two_actual_utc_hours(self):
        ledger = normalize(
            [
                reading("2026-10-25T03:15:00+01:00", 100.3, 0.2),
                reading("2026-10-25T02:15:00+01:00", 100.1, 0.1),
            ]
        )
        points = statistics.build_statistics(ledger)
        self.assertEqual(len(points), 2)
        self.assertEqual([point["start"].hour for point in points], [0, 1])
        self.assertEqual(points[-1]["sum"], 0.3)

    def test_daily_provider_labels_do_not_collide_when_actual_instant_crosses_midnight(self):
        ledger = normalize(
            [
                reading("2026-10-24T23:01:00+01:00", 100.1, 0.1),
                reading("2026-10-25T23:01:00+01:00", 100.3, 0.2),
            ],
            "DAILY",
        )
        points = statistics.build_statistics(ledger)
        self.assertEqual(len(points), 2)
        self.assertEqual(
            [point["start"].astimezone(MADRID).date() for point in points],
            [date(2026, 10, 24), date(2026, 10, 25)],
        )
        self.assertEqual(next(iter(ledger.values()))["observed_at"], "2026-10-24T23:01:00+01:00")

    def test_daily_spring_labels_and_23_25_hour_days_use_calendar_midnights(self):
        for days, expected_hours in (
            ((date(2026, 3, 29), date(2026, 3, 30)), 23),
            ((date(2026, 10, 25), date(2026, 10, 26)), 25),
        ):
            with self.subTest(days=days):
                values = [
                    reading(f"{day.isoformat()}T23:01:00+01:00", 10 + i, 1)
                    for i, day in enumerate(days)
                ]
                points = statistics.build_statistics(normalize(values, "DAILY"))
                self.assertEqual(
                    points[1]["start"] - points[0]["start"], timedelta(hours=expected_hours)
                )
                self.assertEqual(
                    [point["start"].astimezone(MADRID).date() for point in points], list(days)
                )

    def test_daily_and_hourly_are_normalized_independently(self):
        raw = [reading("2026-10-24T23:01:00+01:00", 100.1, 0.1)]
        daily = normalize(raw, "DAILY")
        hourly = normalize(raw, "HOURLY")
        self.assertNotEqual(set(daily), set(hourly))
        self.assertEqual(len(statistics.build_statistics(daily)), 1)
        self.assertEqual(len(statistics.build_statistics(hourly)), 1)

    def test_invalid_readings_fail_without_becoming_zero(self):
        valid = reading("2026-10-28T10:01:00Z", 10, 0.5)
        variants = [
            {**valid, "datetime": "2026-10-28T10:01:00"},
            {**valid, "datetime": "invalid"},
            {**valid, "datetime": None},
            {**valid, "deltaConsumption": None},
            {key: value for key, value in valid.items() if key != "deltaConsumption"},
        ]
        for field in ("accumulatedConsumption", "deltaConsumption"):
            for value in (True, False, -0.1, float("nan"), float("inf"), "0.5"):
                variants.append({**valid, field: value})
        for variant in variants:
            with self.subTest(variant=variant):
                with self.assertRaises(ValueError):
                    normalize([variant])

    def test_invalid_persisted_ledger_fails_without_mutation(self):
        ledger = normalize([reading("2026-10-28T10:01:00Z", 10, 0.5)])
        damaged = deepcopy(ledger)
        next(iter(damaged.values()))["delta"] = float("nan")
        before = deepcopy(ledger)
        with self.assertRaises(ValueError):
            statistics.merge_ledgers(ledger, damaged)
        self.assertEqual(ledger, before)

    def test_empty_inputs_remain_empty(self):
        self.assertEqual(normalize([]), {})
        self.assertEqual(statistics.merge_ledgers({}, {}), {})
        self.assertEqual(statistics.build_statistics({}), [])


class HourlyCoverageTests(unittest.TestCase):
    """Coverage counts reported UTC buckets against whole local calendar days."""

    @staticmethod
    def reported_hours(start, count):
        return normalize(
            [
                reading(
                    (start + timedelta(hours=hour + 1, minutes=1)).isoformat(),
                    100 + hour / 100,
                    0.01,
                )
                for hour in range(count)
            ]
        )

    def test_empty_ledger_has_no_implied_dates_or_intervals(self):
        self.assertEqual(
            statistics.describe_hourly_coverage({}, MADRID),
            {
                "first_interval": None,
                "last_interval": None,
                "known_intervals": 0,
                "expected_intervals": 0,
                "complete_days": 0,
                "incomplete_days": {},
            },
        )

    def test_complete_spring_and_autumn_days_require_23_and_25_utc_hours(self):
        cases = (
            (datetime(2026, 3, 28, 23, tzinfo=UTC), 23),
            (datetime(2026, 10, 24, 22, tzinfo=UTC), 25),
        )
        for start, hours in cases:
            with self.subTest(start=start):
                ledger = self.reported_hours(start, hours)
                coverage = statistics.describe_hourly_coverage(ledger, MADRID)
                self.assertEqual(coverage["first_interval"], start.isoformat())
                self.assertEqual(
                    coverage["last_interval"], (start + timedelta(hours=hours - 1)).isoformat()
                )
                self.assertEqual(coverage["known_intervals"], hours)
                self.assertEqual(coverage["expected_intervals"], hours)
                self.assertEqual(coverage["complete_days"], 1)
                self.assertEqual(coverage["incomplete_days"], {})

    def test_one_missing_repeated_autumn_hour_is_not_a_complete_24_hour_day(self):
        start = datetime(2026, 10, 24, 22, tzinfo=UTC)
        ledger = self.reported_hours(start, 25)
        del ledger["2026-10-25T01:00:00+00:00"]
        coverage = statistics.describe_hourly_coverage(ledger, MADRID)
        self.assertEqual(coverage["known_intervals"], 24)
        self.assertEqual(coverage["expected_intervals"], 25)
        self.assertEqual(coverage["complete_days"], 0)
        self.assertEqual(
            coverage["incomplete_days"],
            {
                "2026-10-25": {"known": 24, "expected": 25},
            },
        )

    def test_partial_edges_are_listed_but_complete_middle_days_are_compact(self):
        ledger = normalize(
            [
                reading("2026-10-26T23:01:00Z", 100, 0),
                reading("2026-10-28T01:01:00Z", 101, 0.1),
            ]
        )
        full_middle = self.reported_hours(datetime(2026, 10, 26, 23, tzinfo=UTC), 24)
        ledger = statistics.merge_ledgers(ledger, full_middle)
        coverage = statistics.describe_hourly_coverage(ledger, MADRID)
        self.assertEqual(coverage["known_intervals"], 26)
        self.assertEqual(coverage["expected_intervals"], 72)
        self.assertEqual(coverage["complete_days"], 1)
        self.assertEqual(
            coverage["incomplete_days"],
            {
                "2026-10-26": {"known": 1, "expected": 24},
                "2026-10-28": {"known": 1, "expected": 24},
            },
        )
        self.assertNotIn("2026-10-29", coverage["incomplete_days"])

    def test_whole_missing_dates_remain_visible_including_dst_length(self):
        cases = (
            ("2026-03-28T00:01:00Z", "2026-03-29T23:01:00Z", "2026-03-29", 23, 71),
            ("2026-10-23T23:01:00Z", "2026-10-26T00:01:00Z", "2026-10-25", 25, 73),
        )
        for first, last, missing_day, hours, total in cases:
            with self.subTest(missing_day=missing_day):
                ledger = normalize([reading(first, 100, 0), reading(last, 101, 0.1)])
                coverage = statistics.describe_hourly_coverage(ledger, MADRID)
                self.assertEqual(coverage["known_intervals"], 2)
                self.assertEqual(coverage["expected_intervals"], total)
                self.assertEqual(coverage["complete_days"], 0)
                self.assertEqual(
                    coverage["incomplete_days"][missing_day],
                    {
                        "known": 0,
                        "expected": hours,
                    },
                )
                self.assertEqual(len(coverage["incomplete_days"]), 3)

    def test_local_date_is_derived_from_the_actual_hour_not_its_utc_date(self):
        ledger = normalize([reading("2026-10-27T00:01:00Z", 100, 0)])
        coverage = statistics.describe_hourly_coverage(ledger, MADRID)
        self.assertEqual(
            coverage["incomplete_days"],
            {
                "2026-10-27": {"known": 1, "expected": 24},
            },
        )
        self.assertEqual(coverage["known_intervals"], 1, "A reported zero is still known")

    def test_complete_week_does_not_return_a_redundant_map_for_every_day(self):
        ledger = self.reported_hours(datetime(2026, 10, 26, 23, tzinfo=UTC), 7 * 24)
        coverage = statistics.describe_hourly_coverage(ledger, MADRID)
        self.assertEqual(coverage["complete_days"], 7)
        self.assertEqual(coverage["known_intervals"], 168)
        self.assertEqual(coverage["expected_intervals"], 168)
        self.assertEqual(coverage["incomplete_days"], {})

    def test_result_is_json_serializable_without_mutating_or_filling_the_ledger(self):
        ledger = normalize(
            [
                reading("2026-10-27T01:01:00+01:00", 100, 0.1),
                reading("2026-10-28T16:01:00+01:00", 102, 0.3),
            ]
        )
        before = deepcopy(ledger)
        points = statistics.build_statistics(ledger)
        coverage = statistics.describe_hourly_coverage(ledger, MADRID)
        self.assertEqual(json.loads(json.dumps(coverage)), coverage)
        self.assertEqual(ledger, before)
        self.assertEqual(statistics.build_statistics(ledger), points)
        self.assertEqual(len(ledger), 2)

    def test_noncanonical_or_daily_ledger_is_rejected_instead_of_counted(self):
        daily = normalize([reading("2026-10-27T23:01:00+01:00", 100, 0.1)], "DAILY")
        with self.assertRaises(ValueError):
            statistics.describe_hourly_coverage(daily, MADRID)
        with self.assertRaises(ValueError):
            statistics.describe_hourly_coverage({}, None)


class WindowTests(unittest.TestCase):
    def test_daily_api_end_is_inclusive_and_exact_requested_days_are_covered(self):
        self.assertEqual(
            statistics.plan_windows(date(2026, 10, 17), date(2026, 10, 25), "DAILY"),
            [(date(2026, 10, 17), date(2026, 10, 23)), (date(2026, 10, 24), date(2026, 10, 24))],
        )

    def test_hourly_api_end_is_exclusive_with_no_week_rounding(self):
        self.assertEqual(
            statistics.plan_windows(date(2026, 10, 17), date(2026, 10, 25), "HOURLY"),
            [(date(2026, 10, 17), date(2026, 10, 24)), (date(2026, 10, 24), date(2026, 10, 25))],
        )

    def test_single_day_daily_and_hourly_have_distinct_end_semantics(self):
        start, stop = date(2026, 10, 28), date(2026, 10, 29)
        self.assertEqual(statistics.plan_windows(start, stop, "DAILY"), [(start, start)])
        self.assertEqual(statistics.plan_windows(start, stop, "HOURLY"), [(start, stop)])

    def test_empty_and_invalid_windows(self):
        start = date(2026, 10, 28)
        self.assertEqual(statistics.plan_windows(start, start, statistics.Frequency.DAILY), [])
        for args in (
            (start, start - timedelta(days=1), "DAILY"),
            (start, start, "MONTHLY"),
            (datetime(2026, 10, 28), start, "DAILY"),
            (start, start + timedelta(days=1), "DAILY", 0),
            (start, start + timedelta(days=1), "DAILY", True),
        ):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    statistics.plan_windows(*args)


if __name__ == "__main__":
    unittest.main()
