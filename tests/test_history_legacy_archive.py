"""Verified native-prefix archiving without importing an unknown meter baseline."""

from copy import deepcopy
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import json
import unittest

from test_history_statistics import MADRID, UTC, normalize, reading, statistics

SOURCE_ID = "sensor.archived_test_meter"


def source_fixture():
    """A lifetime baseline, 140 measured daily increments and six API overlaps."""
    start = date(2023, 6, 7)
    counter = Decimal("1000.000")
    increments = [Decimal("0.475")] * 139 + [Decimal("0.547")]
    overlaps = [
        Decimal("0.432"),
        Decimal("0.3"),
        Decimal("0.4"),
        Decimal("0.5"),
        Decimal("0.6"),
        Decimal("0.7"),
    ]
    native = []
    api_rows = []
    for index, delta in enumerate([Decimal(0), *increments, *overlaps]):
        day = start + timedelta(days=index)
        counter += delta
        native.append(
            {
                "start": datetime.combine(day, time.min, tzinfo=MADRID).astimezone(UTC).isoformat(),
                "state": float(counter),
                "sum": float(counter),
            }
        )
        if index > len(increments):
            api_rows.append(reading(f"{day}T23:02:00+01:00", float(counter), float(delta)))
    return native, normalize(api_rows, "DAILY")


def archive(native, daily, **kwargs):
    return statistics.build_legacy_daily_prefix(
        native, daily, MADRID, statistic_id=kwargs.get("statistic_id", SOURCE_ID)
    )


class LegacyDailyArchiveTests(unittest.TestCase):
    def setUp(self):
        self.native, self.daily = source_fixture()

    def test_unknown_lifetime_baseline_is_never_imported_as_consumption(self):
        before = deepcopy((self.native, self.daily))
        prefix, summary = archive(self.native, self.daily)
        points = statistics.build_statistics(prefix)
        self.assertEqual(len(prefix), 140)
        self.assertEqual(points[0]["sum"], 0.475)
        self.assertEqual(points[-1]["sum"], 66.572)
        self.assertEqual(summary["baseline"]["state"], 1000.0)
        self.assertIs(summary["baseline"]["consumption_imported"], False)
        self.assertEqual(summary["first_imported_day"], "2023-06-08")
        self.assertEqual(summary["last_imported_day"], "2023-10-25")
        self.assertEqual(summary["first_api_day"], "2023-10-26")
        self.assertEqual(summary["matching_overlap_days"], 6)
        self.assertEqual(summary["imported_consumption_m3"], 66.572)
        self.assertEqual((self.native, self.daily), before)

    def test_archive_digest_and_origin_survive_json_validation_and_energy_projection(self):
        prefix, summary = archive(self.native, self.daily)
        restored = statistics.validate_saved_ledger(json.loads(json.dumps(prefix)), "DAILY", MADRID)
        self.assertEqual(restored, prefix)
        self.assertEqual(statistics.merge_ledgers(prefix), prefix)
        self.assertEqual(len(summary["source_sha256"]), 64)
        first = next(iter(prefix.values()))
        self.assertEqual(first["origin"]["statistic_id"], SOURCE_ID)
        self.assertEqual(first["origin"]["start"], self.native[1]["start"])
        self.assertEqual(first["origin"]["previous_state"], self.native[0]["state"])
        expected_row_digest = hashlib.sha256(
            json.dumps(
                self.native[1], sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
        self.assertEqual(first["origin"]["row_sha256"], expected_row_digest)
        energy, _ = statistics.build_energy_projection(prefix, {}, MADRID, hourly_since=None)
        self.assertEqual(statistics.build_statistics(energy)[-1]["sum"], 66.572)
        self.assertEqual(next(iter(energy.values()))["origin"], first["origin"])

    def test_existing_api_rows_remain_identical_and_overlap_is_never_added_twice(self):
        prefix, _ = archive(self.native, self.daily)
        combined = statistics.merge_ledgers(prefix, self.daily)
        self.assertEqual(len(combined), 146)
        self.assertEqual({key: combined[key] for key in self.daily}, self.daily)
        self.assertEqual(statistics.build_statistics(combined)[-1]["sum"], 69.504)

    def test_repeated_archive_ignores_existing_origin_rows_when_finding_api_cutoff(self):
        prefix, summary = archive(self.native, self.daily)
        repeated, proof = archive(list(reversed(self.native)), prefix | self.daily)
        self.assertEqual(repeated, prefix)
        self.assertEqual(proof, summary)

    def test_later_api_observation_naturally_supersedes_archived_midnight(self):
        prefix, _ = archive(self.native, self.daily)
        before = deepcopy(prefix)
        day = date(2023, 6, 8)
        correction = normalize([reading(f"{day}T23:02:00+01:00", 1000.479, 0.479)], "DAILY")
        merged = statistics.reconcile_intervals(
            prefix, correction, "DAILY", MADRID, startdate=day, enddate=day + timedelta(days=1)
        )
        key = next(iter(correction))
        self.assertEqual(merged[key], correction[key])
        self.assertNotIn("origin", merged[key])
        self.assertEqual(
            {k: row for k, row in merged.items() if k != key},
            {k: row for k, row in before.items() if k != key},
        )
        self.assertEqual(prefix, before)

    def test_native_start_is_converted_to_ha_timezone_before_calendar_label(self):
        self.assertEqual(datetime.fromisoformat(self.native[0]["start"]).date(), date(2023, 6, 6))
        prefix, summary = archive(self.native, self.daily)
        self.assertEqual(summary["baseline"]["day"], "2023-06-07")
        first = next(iter(prefix.values()))
        self.assertEqual(
            datetime.fromisoformat(first["observed_at"]), datetime(2023, 6, 8, tzinfo=MADRID)
        )

    def test_zero_consumption_is_a_verified_increment_not_a_missing_date(self):
        self.native[1]["state"] = self.native[0]["state"]
        self.native[1]["sum"] = self.native[0]["sum"]
        prefix, _ = archive(self.native, self.daily)
        self.assertEqual(next(iter(prefix.values()))["delta"], 0)
        self.assertEqual(len(prefix), 140)
        self.assertEqual(statistics.build_statistics(prefix)[-1]["sum"], 66.572)

    def test_duplicate_gap_reset_and_state_sum_disagreement_fail_without_mutation(self):
        cases = {}
        duplicate = deepcopy(self.native)
        duplicate.insert(2, deepcopy(duplicate[1]))
        cases["duplicate"] = duplicate
        gap = deepcopy(self.native)
        gap.pop(3)
        cases["gap"] = gap
        reset = deepcopy(self.native)
        reset[2]["state"] = reset[2]["sum"] = 0
        cases["reset"] = reset
        disagreement = deepcopy(self.native)
        disagreement[2]["sum"] += 0.001
        cases["sum"] = disagreement
        for label, native in cases.items():
            with self.subTest(label=label):
                before = deepcopy((native, self.daily))
                with self.assertRaises(ValueError):
                    archive(native, self.daily)
                self.assertEqual((native, self.daily), before)

    def test_api_state_or_delta_mismatch_rejects_the_entire_prefix(self):
        for field in ("state", "delta"):
            with self.subTest(field=field):
                daily = deepcopy(self.daily)
                next(iter(daily.values()))[field] += 0.001
                before = deepcopy((self.native, daily))
                with self.assertRaisesRegex(ValueError, "overlap"):
                    archive(self.native, daily)
                self.assertEqual((self.native, daily), before)

    def test_three_consecutive_real_api_overlap_days_are_required(self):
        items = list(self.daily.items())
        for daily in (dict(items[:2]), dict(items[::2])):
            with self.subTest(dates=list(daily)):
                with self.assertRaisesRegex(ValueError, "three consecutive"):
                    archive(self.native, daily)
        prefix, _ = archive(self.native, dict(items[:3]))
        self.assertEqual(len(prefix), 140)

    def test_another_archive_cannot_be_used_as_the_only_api_validation(self):
        prefix, _ = archive(self.native, self.daily)
        with self.assertRaisesRegex(ValueError, "Actual API"):
            archive(self.native, prefix)

    def test_missing_bad_or_non_native_input_fails_closed(self):
        for native in (None, {}, [], self.native[:2]):
            with self.subTest(native_type=type(native).__name__), self.assertRaises(ValueError):
                archive(native, self.daily)
        for statistic_id in ("", "aigues_barcelona_history:meter", "sensor.bad id", None):
            with self.subTest(statistic_id=statistic_id), self.assertRaises(ValueError):
                archive(self.native, self.daily, statistic_id=statistic_id)

    def test_invalid_native_numbers_and_naive_timestamps_are_rejected(self):
        for field, value in (
            ("state", float("nan")),
            ("sum", float("inf")),
            ("state", True),
            ("sum", -1),
            ("start", "2023-06-07T00:00:00"),
        ):
            with self.subTest(field=field, value=value):
                native = deepcopy(self.native)
                native[0][field] = value
                with self.assertRaises(ValueError):
                    archive(native, self.daily)


class ArchiveOriginValidationTests(unittest.TestCase):
    def setUp(self):
        native, daily = source_fixture()
        self.prefix, _ = archive(native, daily)

    def test_invalid_origin_fields_fail_saved_ledger_validation(self):
        first = next(iter(self.prefix.values()))["origin"]
        invalid = [None, [], {}, {**first, "extra": "unapproved"}]
        for key, value in (
            ("type", "provider"),
            ("statistic_id", "history:meter"),
            ("start", "2023-06-08"),
            ("row_sha256", "x" * 64),
            ("row_sha256", "a" * 63),
            ("row_sha256", "a" * 64),
            ("previous_state", None),
            ("previous_state", True),
            ("previous_state", -1),
            ("previous_state", float("inf")),
            ("previous_state", float("nan")),
        ):
            invalid.append({**first, key: value})
        for origin in invalid:
            with self.subTest(origin=origin):
                ledger = deepcopy(self.prefix)
                next(iter(ledger.values()))["origin"] = origin
                with self.assertRaises(ValueError):
                    statistics.validate_saved_ledger(ledger, "DAILY", MADRID)

    def test_changed_archived_state_cannot_keep_the_original_row_digest(self):
        ledger = deepcopy(self.prefix)
        next(iter(ledger.values()))["state"] += 0.001
        with self.assertRaisesRegex(ValueError, "digest"):
            statistics.merge_ledgers(ledger)

    def test_changed_archived_delta_cannot_keep_the_verified_meter_difference(self):
        ledger = deepcopy(self.prefix)
        next(iter(ledger.values()))["delta"] = 0.476
        before = deepcopy(ledger)
        with self.assertRaisesRegex(ValueError, "meter difference"):
            statistics.validate_saved_ledger(ledger, "DAILY", MADRID)
        self.assertEqual(ledger, before)

    def test_changed_previous_state_cannot_keep_the_verified_meter_difference(self):
        for previous_state in (1000.001, 1001):
            with self.subTest(previous_state=previous_state):
                ledger = deepcopy(self.prefix)
                next(iter(ledger.values()))["origin"]["previous_state"] = previous_state
                before = deepcopy(ledger)
                with self.assertRaisesRegex(ValueError, "meter difference"):
                    statistics.validate_saved_ledger(ledger, "DAILY", MADRID)
                self.assertEqual(ledger, before)

    def test_archive_date_and_midnight_label_must_match_its_native_origin(self):
        original = next(iter(self.prefix.values()))
        for observed in ("2023-06-09T00:00:00+02:00", "2023-06-08T12:00:00+02:00"):
            with self.subTest(observed=observed):
                record = deepcopy(original)
                record["observed_at"] = observed
                record["start"] = statistics.canonical_start(
                    datetime.fromisoformat(observed), "DAILY", MADRID
                ).isoformat()
                with self.assertRaisesRegex(ValueError, "origin"):
                    statistics.validate_saved_ledger({record["start"]: record}, "DAILY", MADRID)
        hourly = deepcopy(original)
        hourly["start"] = statistics.canonical_start(
            datetime.fromisoformat(hourly["observed_at"]), "HOURLY", MADRID
        ).isoformat()
        with self.assertRaisesRegex(ValueError, "origin"):
            statistics.validate_saved_ledger({hourly["start"]: hourly}, "HOURLY", MADRID)

    def test_origin_without_changes_does_not_affect_normal_api_source_records(self):
        _, daily = source_fixture()
        before = json.dumps(daily, sort_keys=True)
        restored = statistics.validate_saved_ledger(daily, "DAILY", MADRID)
        self.assertEqual(json.dumps(restored, sort_keys=True), before)


if __name__ == "__main__":
    unittest.main()
