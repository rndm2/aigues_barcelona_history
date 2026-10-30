"""Independent ownership, precision and HA aggregation tests for Energy."""

from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
import unittest

from test_history_statistics import MADRID, normalize, reading, statistics

CET = statistics.PROVIDER_TIME_ZONE


def daily_report(day, amount, *, state=1000, observed_hour=23):
    return normalize([reading(f"{day}T{observed_hour:02}:02:00+01:00", state, amount)], "DAILY")


def hourly_report(day, *, hours=range(24), deltas=None, state=1000):
    """Build raw end readings without using the projection's block helpers."""
    deltas = [Decimal("0.001")] * 24 if deltas is None else [Decimal(str(v)) for v in deltas]
    counter = Decimal(str(state))
    rows = []
    for hour, delta in enumerate(deltas):
        counter += delta
        if hour in hours:
            rows.append(reading(f"{day}T{hour:02}:02:00+01:00", float(counter), float(delta)))
    return normalize(rows)


def project(daily=None, hourly=None, *, since=date(2026, 6, 12), use_daily=True):
    return statistics.build_energy_projection(
        daily or {},
        hourly or {},
        MADRID,
        hourly_since=since,
        use_daily_totals_for_incomplete=use_daily,
    )


def local_totals(ledger, period="day"):
    result = defaultdict(lambda: Decimal(0))
    for row in ledger.values():
        local = datetime.fromisoformat(row["start"]).astimezone(MADRID)
        key = local.date().isoformat() if period == "day" else local.strftime("%Y-%m")
        result[key] += Decimal(str(row["delta"]))
    return dict(result)


class EnergyFrontierTests(unittest.TestCase):
    def test_no_hours_have_no_frontier(self):
        self.assertIsNone(statistics.initial_energy_hourly_since({}, MADRID))

    def test_first_complete_provider_block_follows_initial_partial_day(self):
        partial = hourly_report(date(2026, 6, 11), hours=range(7, 24))
        complete = hourly_report(date(2026, 6, 12))
        self.assertEqual(
            statistics.initial_energy_hourly_since(partial | complete, MADRID),
            date(2026, 6, 12),
        )

    def test_only_partial_hours_use_earliest_provider_date(self):
        rows = hourly_report(date(2026, 1, 10), hours=[0]) | hourly_report(
            date(2026, 1, 12), hours=[23]
        )
        self.assertEqual(statistics.initial_energy_hourly_since(rows, MADRID), date(2026, 1, 10))


class EnergyProjectionTests(unittest.TestCase):
    def test_initial_sum_uses_daily_amount_not_lifetime_meter_or_uniform_hours(self):
        source = daily_report(date(2026, 6, 11), 0.559, state=900)
        ledger, coverage = project(source)
        points = statistics.build_statistics(ledger)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["sum"], 0.559)
        self.assertEqual(points[0]["state"], 900)
        self.assertEqual(
            points[0]["start"].astimezone(MADRID), datetime(2026, 6, 11, 23, tzinfo=MADRID)
        )
        self.assertEqual(coverage["daily_aggregate_dates"], ["2026-06-11"])
        self.assertEqual(coverage["reported_hours"], 0)

    def test_old_partial_june_day_uses_daily_without_double_counting_known_hours(self):
        daily = daily_report(date(2026, 6, 11), 0.559)
        partial = hourly_report(date(2026, 6, 11), hours=range(7, 24))
        complete = hourly_report(date(2026, 6, 12))
        ledger, coverage = project(daily, partial | complete, use_daily=False)
        self.assertEqual(len(ledger), 25)
        self.assertEqual(statistics.build_statistics(ledger)[-1]["sum"], 0.583)
        self.assertEqual(coverage["suppressed_reported_hours"], 17)
        self.assertEqual(coverage["reported_hours"], 24)
        self.assertEqual(
            local_totals(ledger), {"2026-06-11": Decimal("0.559"), "2026-06-12": Decimal("0.024")}
        )

    def test_both_policies_are_explicit_for_recent_partial_block(self):
        day = date(2026, 10, 27)
        daily = daily_report(day, 0.739)
        hours = hourly_report(day, hours=[0, 1], deltas=[0.014, 0.016] + [0] * 22)
        coarse, coarse_coverage = project(daily, hours)
        actual, actual_coverage = project(daily, hours, use_daily=False)
        self.assertEqual(len(coarse), 1)
        self.assertEqual(statistics.build_statistics(coarse)[-1]["sum"], 0.739)
        self.assertEqual(actual, hours)
        self.assertEqual(statistics.build_statistics(actual)[-1]["sum"], 0.030)
        self.assertEqual(coarse_coverage["suppressed_reported_hours"], 2)
        self.assertEqual(actual_coverage["daily_aggregate_dates"], [])
        self.assertEqual(actual_coverage["reported_hours"], 2)

    def test_no_hours_always_allow_coarse_data_even_after_frontier(self):
        ledger, coverage = project(daily_report(date(2026, 10, 27), 0.739), use_daily=False)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(coverage["daily_aggregate_dates"], ["2026-10-27"])

    def test_missing_daily_keeps_actual_partial_hours_without_counter_interpolation(self):
        hours = hourly_report(date(2026, 1, 10), hours=[0, 23])
        ledger, coverage = project(hourly=hours)
        self.assertEqual(ledger, hours)
        self.assertEqual(statistics.build_statistics(ledger)[-1]["sum"], 0.002)
        self.assertEqual(coverage["reported_hours"], 2)

    def test_daily_below_known_amount_retains_actual_hours_under_both_policies(self):
        day = date(2026, 1, 10)
        hours = hourly_report(day, hours=[0, 1], deltas=[0.2, 0.3] + [0] * 22)
        for use_daily in (True, False):
            with self.subTest(use_daily=use_daily):
                ledger, coverage = project(daily_report(day, 0.4), hours, use_daily=use_daily)
                self.assertEqual(ledger, hours)
                self.assertEqual(coverage["disagreement_dates"], [day.isoformat()])
                self.assertEqual(coverage["suppressed_reported_hours"], 0)

    def test_complete_hours_are_authoritative_even_if_daily_total_disagrees(self):
        day = date(2026, 10, 23)
        hours = hourly_report(day)
        ledger, coverage = project(daily_report(day, 0.025), hours)
        self.assertEqual(ledger, hours)
        self.assertEqual(coverage["disagreement_dates"], [day.isoformat()])
        self.assertEqual(coverage["daily_aggregate_dates"], [])

    def test_complete_older_island_does_not_hide_other_daily_history(self):
        first = date(2026, 1, 10)
        daily = {}
        for offset in range(3):
            daily |= daily_report(first + timedelta(days=offset), 0.024)
        hours = hourly_report(first)
        ledger, coverage = project(daily, hours, use_daily=False)
        self.assertEqual(len(ledger), 26)
        self.assertEqual(coverage["daily_aggregate_dates"], ["2026-01-11", "2026-01-12"])
        self.assertEqual(coverage["reported_hours"], 24)

    def test_source_ledgers_and_persisted_frontier_are_immutable_and_repeatable(self):
        daily = daily_report(date(2026, 6, 11), 0.559)
        hourly = hourly_report(date(2026, 6, 11), hours=range(7, 24))
        before = deepcopy((daily, hourly))
        first = project(daily, hourly)
        self.assertEqual(first, project(daily, hourly))
        self.assertEqual((daily, hourly), before)
        older = hourly_report(date(2026, 1, 10))
        _, coverage = project(daily, older | hourly)
        self.assertEqual(coverage["hourly_since"], "2026-06-12")

    def test_whole_june_month_combines_coarse_history_and_real_hours_once(self):
        daily = {}
        for number in range(1, 31):
            daily |= daily_report(date(2026, 6, number), 0.024)
        partial = hourly_report(date(2026, 6, 11), hours=range(7, 24))
        full = hourly_report(date(2026, 6, 12))
        ledger, coverage = project(daily, partial | full, use_daily=False)
        self.assertEqual(local_totals(ledger, "month"), {"2026-06": Decimal("0.720")})
        self.assertEqual(coverage["daily_aggregate_days"], 29)
        self.assertEqual(coverage["complete_hourly_blocks"], 1)
        self.assertEqual(coverage["reported_hours"], 24)
        self.assertEqual(coverage["suppressed_reported_hours"], 17)

    def test_scoped_source_correction_changes_only_its_consumption_and_later_sums(self):
        first_day = date(2026, 10, 22)
        before_hours = hourly_report(first_day) | hourly_report(first_day + timedelta(days=1))
        before, _ = project(hourly=before_hours)
        correction = hourly_report(
            first_day, hours=[12], deltas=[0.001] * 12 + [0.010] + [0.001] * 11
        )
        updated = statistics.reconcile_intervals(
            before_hours,
            correction,
            "HOURLY",
            MADRID,
            startdate=first_day,
            enddate=first_day + timedelta(days=1),
        )
        after, _ = project(hourly=updated)
        target = next(iter(correction))
        self.assertEqual(
            {key: row for key, row in after.items() if key != target},
            {key: row for key, row in before.items() if key != target},
        )
        previous_points = statistics.build_statistics(before)
        new_points = statistics.build_statistics(after)
        self.assertEqual(new_points[-1]["sum"], 0.057)
        self.assertEqual(previous_points[-1]["sum"], 0.048)
        self.assertEqual(after, project(hourly=updated)[0])

    def test_complete_backfill_reuses_coarse_anchor_and_preserves_total(self):
        day = date(2026, 6, 11)
        daily = daily_report(day, 0.559)
        deltas = [0.001] * 23 + [0.536]
        partial = hourly_report(day, hours=[0], deltas=deltas)
        before, _ = project(daily, partial)
        complete = hourly_report(day, deltas=deltas)
        after, coverage = project(daily, complete)
        self.assertEqual(len(before), 1)
        self.assertEqual(len(after), 24)
        self.assertLessEqual(before.keys(), after.keys())
        self.assertEqual(statistics.build_statistics(before)[-1]["sum"], 0.559)
        self.assertEqual(statistics.build_statistics(after)[-1]["sum"], 0.559)
        self.assertEqual(coverage["daily_aggregate_dates"], [])

    def test_recent_policy_change_can_remove_an_aggregate_key_and_requires_republication(self):
        day = date(2026, 10, 27)
        daily = daily_report(day, 0.739)
        partial = hourly_report(day, hours=[0, 1])
        before, _ = project(daily, partial)
        after, _ = project(daily, partial, use_daily=False)
        self.assertTrue(before.keys() - after.keys())
        self.assertEqual(after, partial)

    def test_new_hourly_contradiction_replaces_coarse_with_actual_partial_without_negative_residual(
        self,
    ):
        day = date(2026, 10, 27)
        daily = daily_report(day, 0.1)
        before, _ = project(daily, hourly_report(day, hours=[0]))
        after, coverage = project(
            daily, hourly_report(day, hours=[0, 1], deltas=[0.1, 0.1] + [0] * 22)
        )
        self.assertTrue(before.keys() - after.keys())
        self.assertEqual(len(after), 2)
        self.assertEqual(statistics.build_statistics(after)[-1]["sum"], 0.2)
        self.assertEqual(coverage["disagreement_dates"], [day.isoformat()])

    def test_precision_details_are_bounded_while_counts_and_date_span_remain_complete(self):
        daily = {}
        partial = {}
        full = {}
        for offset in range(30):
            day = date(2026, 1, 1) + timedelta(days=offset)
            daily |= daily_report(day, 0.1)
            partial |= hourly_report(day, hours=[0])
            full |= hourly_report(day)
        _, coverage = project(daily, partial)
        self.assertEqual(coverage["daily_aggregate_days"], 30)
        self.assertEqual(coverage["first_daily_aggregate_date"], "2026-01-01")
        self.assertEqual(coverage["last_daily_aggregate_date"], "2026-01-30")
        self.assertEqual(len(coverage["daily_aggregate_dates"]), 14)
        self.assertEqual(coverage["incomplete_provider_days"], 30)
        self.assertEqual(len(coverage["incomplete_provider_dates"]), 14)
        _, conflicts = project(daily, full)
        self.assertEqual(conflicts["disagreement_days"], 30)
        self.assertEqual(len(conflicts["disagreement_dates"]), 14)

    def test_zero_daily_report_is_retained_without_fabricated_hours(self):
        ledger, coverage = project(daily_report(date(2026, 1, 10), 0, state=0))
        self.assertEqual(len(ledger), 1)
        self.assertEqual(statistics.build_statistics(ledger)[0]["sum"], 0)
        self.assertEqual(coverage["reported_hours"], 0)

    def test_invalid_sources_and_policy_fail_before_projection(self):
        for since, policy in ((datetime(2026, 1, 1), True), ("2026-01-01", True), (None, 1)):
            with self.subTest(since=since, policy=policy), self.assertRaises(ValueError):
                project(since=since, use_daily=policy)
        invalid = hourly_report(date(2026, 1, 10), hours=[0])
        next(iter(invalid.values()))["delta"] = -1
        with self.assertRaises(ValueError):
            project(hourly=invalid)


class EnergyBoundaryTests(unittest.TestCase):
    def test_winter_month_handoff_retains_the_real_boundary_hour_on_its_civil_date(self):
        coarse_day = date(2026, 1, 31)
        fine_day = date(2026, 2, 1)
        ledger, _ = project(daily_report(coarse_day, 0.1), hourly_report(fine_day))
        # The first fine provider-date reading ends at Feb1 00 CET. It closes
        # Jan31 23:00, directly after the coarse block's final 22:00 anchor.
        # Keeping that real hour in January prevents a physical interval gap.
        self.assertEqual(
            local_totals(ledger, "month"),
            {"2026-01": Decimal("0.101"), "2026-02": Decimal("0.023")},
        )
        self.assertEqual(statistics.build_statistics(ledger)[-1]["sum"], 0.124)

    def test_coarse_and_fine_provider_blocks_meet_without_overlap_in_all_seasons(self):
        for day in (date(2026, 1, 9), date(2026, 3, 28), date(2026, 6, 11), date(2026, 10, 24)):
            with self.subTest(day=day):
                daily = daily_report(day, 0.1)
                hours = hourly_report(day + timedelta(days=1))
                ledger, _ = project(daily, hours)
                aggregate = next(row for key, row in ledger.items() if key not in hours)
                coarse_start = datetime.fromisoformat(aggregate["start"])
                first_fine = min(datetime.fromisoformat(key) for key in hours)
                self.assertEqual(coarse_start + timedelta(hours=1), first_fine)
                self.assertEqual(coarse_start.astimezone(MADRID).date(), day)
                self.assertEqual(statistics.build_statistics(ledger)[-1]["sum"], 0.124)

    def test_coarse_dates_remain_in_the_provider_month_through_dst_and_year_boundary(self):
        days = [date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30), date(2026, 10, 31)]
        daily = {}
        for day in days:
            daily |= daily_report(day, 0.1, observed_hour=18)
        ledger, _ = project(daily)
        self.assertEqual(
            local_totals(ledger, "month"), {day.strftime("%Y-%m"): Decimal("0.1") for day in days}
        )

    def test_real_dst_days_retain_23_or_25_intervals_in_one_energy_series(self):
        for day, count in ((date(2026, 3, 29), 23), (date(2026, 10, 25), 25)):
            with self.subTest(day=day):
                hours = hourly_report(day) | hourly_report(day + timedelta(days=1))
                ledger, coverage = project(hourly=hours)
                local = [datetime.fromisoformat(key).astimezone(MADRID) for key in ledger]
                selected = [stamp for stamp in local if stamp.date() == day]
                self.assertEqual(len(selected), count)
                self.assertEqual(local_totals(ledger)[day.isoformat()], Decimal(count) / 1000)
                repeated = [stamp for stamp in selected if stamp.hour == 2]
                self.assertEqual(len(repeated), 0 if count == 23 else 2)
                if count == 25:
                    self.assertEqual(
                        {stamp.utcoffset() for stamp in repeated},
                        {timedelta(hours=1), timedelta(hours=2)},
                    )
                self.assertEqual(coverage["daily_aggregate_dates"], [])
                self.assertEqual(coverage["reported_hours"], 48)

    def test_equivalent_iso_offsets_cannot_move_an_hour_to_another_provider_block(self):
        hours = normalize([reading("2026-10-10T00:02:00+02:00", 100, 0.1)])
        daily = daily_report(date(2026, 10, 9), 0.2)
        ledger, coverage = project(daily, hours)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(coverage["daily_aggregate_dates"], ["2026-10-09"])
        self.assertEqual(coverage["suppressed_reported_hours"], 1)


class EnergyActualHAViewsTests(unittest.TestCase):
    def test_actual_ha_monthly_and_daily_changes_share_one_mixed_precision_source(self):
        from homeassistant.components.recorder import statistics as ha
        from homeassistant.components.recorder.db_schema import Statistics
        from test_history_integrity import RecorderSQLiteIntegrityTests

        fixture = RecorderSQLiteIntegrityTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        daily = {}
        for day in range(1, 32):
            daily |= daily_report(date(2026, 1, day), 0.1)
        daily |= daily_report(date(2026, 6, 11), 0.559)
        ledger, _ = project(daily, hourly_report(date(2026, 6, 12)))
        target = "aigues_barcelona_history:test123_energy_consumption"
        metadata = {
            **fixture.coordinator._statistic_metadata(statistics.Frequency.HOURLY),
            "statistic_id": target,
        }
        ha.import_statistics(
            fixture.queue, metadata, statistics.build_statistics(ledger), Statistics
        )
        start = datetime(2026, 1, 1, tzinfo=MADRID)
        stop = datetime(2026, 7, 1, tzinfo=MADRID) - timedelta(microseconds=1)
        expected = {"2026-01": 3100, "2026-06": 583}
        months = ha.statistics_during_period(
            fixture.coordinator.hass, start, stop, {target}, "month", {"volume": "L"}, {"change"}
        )[target]
        self.assertEqual(
            {
                datetime.fromtimestamp(row["start"], MADRID).strftime("%Y-%m"): round(row["change"])
                for row in months
            },
            expected,
        )
        days = ha.statistics_during_period(
            fixture.coordinator.hass, start, stop, {target}, "day", {"volume": "L"}, {"change"}
        )[target]
        by_month = defaultdict(float)
        for row in days:
            by_month[datetime.fromtimestamp(row["start"], MADRID).strftime("%Y-%m")] += row[
                "change"
            ]
        self.assertEqual({key: round(value) for key, value in by_month.items()}, expected)
        self.assertEqual(fixture.database_rows(fixture.old_id), fixture.old_snapshot)


if __name__ == "__main__":
    unittest.main()
