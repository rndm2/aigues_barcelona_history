"""Scoped reconciliation regressions for partial and corrected provider data."""

from copy import deepcopy
from datetime import date
import unittest

from test_history_statistics import MADRID, normalize, reading, statistics


class HourlyReconciliationTests(unittest.TestCase):
    def reconcile(self, current, incoming):
        return statistics.reconcile_intervals(
            current,
            incoming,
            "HOURLY",
            MADRID,
            startdate=date(2026, 10, 23),
            enddate=date(2026, 10, 24),
        )

    def test_partial_response_adds_hours_and_corrects_values_without_deletion(self):
        original = normalize(
            [
                reading("2026-10-22T23:01:00+01:00", 100, 0.015),
                reading("2026-10-23T00:02:00+01:00", 101, 0.017),
                reading("2026-10-23T03:01:00+01:00", 102, 0.1),
            ]
        )
        before = deepcopy(original)
        partial = normalize(
            [
                reading("2026-10-23T00:01:00+01:00", 101.1, 0.027),
                reading("2026-10-23T01:01:00+01:00", 101.2, 0.024),
            ]
        )
        result = self.reconcile(original, partial)
        self.assertEqual(len(result), 4)
        self.assertEqual(
            [row["delta"] for _, row in sorted(result.items())], [0.015, 0.027, 0.024, 0.1]
        )
        self.assertEqual(original, before)
        self.assertEqual(self.reconcile(result, partial), result)
        self.assertEqual(self.reconcile(result, {}), result)

    def test_global_ha_bounds_protect_outside_intervals_from_padded_responses(self):
        original = normalize(
            [
                reading("2026-10-22T22:01:00+01:00", 100, 0.2),
                reading("2026-10-23T00:01:00+01:00", 101, 0.3),
                reading("2026-10-24T00:01:00+01:00", 102, 0.4),
            ]
        )
        update = normalize(
            [
                reading("2026-10-22T22:01:00+01:00", 200, 9),
                reading("2026-10-23T00:01:00+01:00", 201, 0.5),
                reading("2026-10-24T00:01:00+01:00", 202, 9),
            ]
        )
        result = self.reconcile(original, update)
        keys = sorted(result)
        self.assertEqual(result[keys[0]], original[keys[0]])
        self.assertEqual(result[keys[-1]], original[keys[-1]])
        self.assertEqual(result[keys[1]]["delta"], 0.5)
        points = statistics.build_statistics(result)
        self.assertEqual([point["sum"] for point in points], [0.2, 0.7, 1.1])
        self.assertAlmostEqual(points[-1]["sum"] - points[-2]["sum"], 0.4)

    def test_normalization_filters_by_ha_day_before_resolving_duplicates(self):
        result = statistics.normalize_readings(
            [
                reading("2026-10-23T00:01:00+01:00", 100, 0.015),
                reading("2026-10-24T00:01:00+01:00", 101, 0.5),
            ],
            "HOURLY",
            MADRID,
            startdate=date(2026, 10, 23),
            enddate=date(2026, 10, 24),
        )
        self.assertEqual(list(result), ["2026-10-22T22:00:00+00:00"])
        self.assertEqual(next(iter(result.values()))["delta"], 0.015)
