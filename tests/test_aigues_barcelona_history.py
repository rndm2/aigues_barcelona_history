"""Offline History orchestration tests using real HA validators and fake IO."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, datetime
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, call, patch
from zoneinfo import ZoneInfo

from homeassistant.components.energy import validate as energy_validate
from homeassistant.components.recorder import statistics as recorder_statistics
from homeassistant.components.sensor import SensorStateClass
from homeassistant.components.sensor import recorder as sensor_recorder
from homeassistant.const import CONF_STATE, UnitOfVolume
from homeassistant.core import CoreState, State
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util.unit_conversion import VolumeConverter
import yaml

from custom_components import aigues_barcelona_history as history_integration
from custom_components.aigues_barcelona_history import config_flow, sensor, service
from custom_components.aigues_barcelona_history.api import (
    AiguesApiAuthError,
    AiguesApiServerError,
    AiguesBarcelonaHistoryApiClient,
)
from custom_components.aigues_barcelona_history.const import (
    CONF_CONTRACT,
    CONF_HISTORY_DAYS,
    CONF_SHOULD_IMPORT_HISTORY,
    CONF_VALUE,
    DOMAIN,
    NAME,
)
from custom_components.aigues_barcelona_history.statistics import Frequency

CONTRACT = "TEST123"
OBJECT_ID = CONTRACT.lower()
DAILY_ID = f"{DOMAIN}:{OBJECT_ID}_consumption"
HOURLY_ID = f"{DOMAIN}:{OBJECT_ID}_hourly_consumption"
ENERGY_ID = f"{DOMAIN}:{OBJECT_ID}_energy_consumption"
MADRID = ZoneInfo("Europe/Madrid")
NOW = datetime(2026, 10, 29, 17, 45, tzinfo=MADRID)


def reading(timestamp, state, delta):
    return {"datetime": timestamp, "accumulatedConsumption": state, "deltaConsumption": delta}


async def execute(function, *args):
    result = function(*args)
    return await result if inspect.isawaitable(result) else result


class FakeStore:
    """Copied JSON values only, never an actual HA storage file."""

    def __init__(self, saved=None):
        self.saved = deepcopy(saved)
        self.async_load = AsyncMock(side_effect=self.load)
        self.async_save = AsyncMock(side_effect=self.save)

    async def load(self):
        return deepcopy(self.saved)

    async def save(self, value):
        self.saved = deepcopy(value)


def make_coordinator(store=None):
    coordinator = object.__new__(sensor.AiguesHistoryCoordinator)
    entry = SimpleNamespace(
        entry_id="history-entry",
        data={"username": "unused", "password": "unused", "twocaptcha_api_key": "unused"},
        options={CONF_SHOULD_IMPORT_HISTORY: False},
    )

    def update_entry(target, **changes):
        for key, value in changes.items():
            setattr(target, key, value)

    coordinator.hass = SimpleNamespace(
        async_add_executor_job=execute,
        config_entries=SimpleNamespace(
            async_get_entry=Mock(return_value=entry),
            async_entries=Mock(return_value=[]),
            async_update_entry=Mock(side_effect=update_entry),
        ),
    )
    coordinator.contract, coordinator.id = CONTRACT, OBJECT_ID
    coordinator.sensor_entity_id = f"sensor.{DOMAIN}_{OBJECT_ID}"
    coordinator.sensor_unique_id = f"{DOMAIN}_{OBJECT_ID}_meter"
    coordinator.statistic_ids = {Frequency.DAILY: DAILY_ID, Frequency.HOURLY: HOURLY_ID}
    coordinator.energy_statistic_id = ENERGY_ID
    coordinator._energy_hourly_since = None
    coordinator._energy_projection_initialized = False
    coordinator._hourly_backfill_attempts = {}
    coordinator._hourly_requested_ranges = []
    coordinator.hourly_import_start = NOW.date()
    coordinator.hourly_import_end = NOW.date()
    coordinator.entry_id = entry.entry_id
    coordinator._data = {"import_status": sensor._empty_import_status()}
    coordinator._ledger = None
    coordinator._stopping = False
    coordinator._manual_import_reserved = False
    coordinator._store = store or FakeStore()
    coordinator._lock, coordinator._token_lock = asyncio.Lock(), asyncio.Lock()
    coordinator._request_gate = {"lock": asyncio.Lock(), "next_request_at": 0}
    coordinator._history_days, coordinator._should_import_history = 7, False
    coordinator.last_update_success = True
    coordinator._ensure_token = AsyncMock()
    coordinator.async_set_updated_data = Mock()
    coordinator.async_update_listeners = Mock()
    coordinator._api = Mock(spec=AiguesBarcelonaHistoryApiClient)
    coordinator._api.is_token_expired.return_value = False
    coordinator._api.consumptions.return_value = []
    return coordinator


class OfflineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(
            patch("requests.Session.request", side_effect=AssertionError("Network disabled"))
        )
        self.enterContext(
            patch("socket.create_connection", side_effect=AssertionError("Network disabled"))
        )
        self.enterContext(
            patch.object(
                AiguesBarcelonaHistoryApiClient,
                "login",
                side_effect=AssertionError("Login disabled"),
            )
        )
        self.enterContext(patch.object(sensor.dt_util, "DEFAULT_TIME_ZONE", MADRID))
        self.enterContext(patch.object(sensor.dt_util, "now", return_value=NOW))
        self.enterContext(patch.object(sensor, "MIN_REQUEST_INTERVAL_SECONDS", 0))
        self.recorded = {}
        self.recorded_metadata = {}
        self.queue = SimpleNamespace(
            async_import_statistics=Mock(side_effect=self.record_points),
            async_clear_statistics=Mock(side_effect=self.clear_statistics),
            async_block_till_done=AsyncMock(),
            async_add_executor_job=execute,
        )
        self.enterContext(
            patch.object(recorder_statistics, "get_instance", return_value=self.queue)
        )
        self.enterContext(patch.object(sensor, "get_instance", return_value=self.queue))
        self.last_statistics = self.enterContext(
            patch.object(
                sensor, "get_last_statistics", side_effect=self.read_points, return_value=None
            )
        )
        self.metadata = self.enterContext(
            patch.object(sensor, "get_metadata", side_effect=self.read_metadata)
        )
        self.coordinator = make_coordinator()

    def clear_statistics(self, statistic_ids):
        for statistic_id in statistic_ids:
            self.recorded.pop(statistic_id, None)
            self.recorded_metadata.pop(statistic_id, None)

    def record_points(self, metadata, points, table):
        self.recorded_metadata[metadata["statistic_id"]] = (123, deepcopy(metadata))
        records = self.recorded.setdefault(metadata["statistic_id"], {})
        for point in points:
            records[point["start"].timestamp()] = {
                "start": point["start"].timestamp(),
                "state": point["state"],
                "sum": point["sum"],
            }

    def read_metadata(self, hass, *, statistic_ids):
        return {
            key: deepcopy(value)
            for key, value in self.recorded_metadata.items()
            if key in statistic_ids
        }

    def read_points(self, hass, count, statistic_id, convert_units, types):
        if isinstance(self.last_statistics.return_value, dict):
            return self.last_statistics.return_value
        records = self.recorded.get(statistic_id, {})
        rows = [deepcopy(records[key]) for key in sorted(records)[-count:]]
        return {statistic_id: rows} if rows else {}

    async def import_frequency(self, frequency, rows):
        self.coordinator._api.consumptions.return_value = rows
        await self.coordinator._import_range(date(2026, 10, 27), date(2026, 10, 29), (frequency,))

    def publications(self):
        return {
            c.args[0]["statistic_id"]: (deepcopy(c.args[0]), deepcopy(c.args[1]), c.args[2])
            for c in self.queue.async_import_statistics.call_args_list
        }

    def publication_count(self, statistic_id):
        return sum(
            call.args[0]["statistic_id"] == statistic_id
            for call in self.queue.async_import_statistics.call_args_list
        )


class CoordinatorLedgerTests(OfflineTests):
    async def test_loads_store_once_and_persists_source_deltas(self):
        await self.import_frequency(
            Frequency.DAILY, [reading("2026-10-27T23:01:28+01:00", 655.724, 1.478)]
        )
        store = self.coordinator._store
        store.async_load.assert_awaited_once_with()
        store.async_save.assert_awaited_once()
        self.assertEqual(
            set(store.saved),
            {
                "DAILY",
                "HOURLY",
                "hourly_interval_version",
                "hourly_replay_pending",
                "energy_projection_version",
                "energy_hourly_since",
                "hourly_backfill_attempts",
                "hourly_requested_ranges",
            },
        )
        self.assertEqual(store.saved["hourly_interval_version"], 2)
        self.assertFalse(store.saved["hourly_replay_pending"])
        self.assertEqual(store.saved["energy_projection_version"], 1)
        self.assertIsNone(store.saved["energy_hourly_since"])
        self.assertEqual(len(store.saved["DAILY"]), 1)
        row = next(iter(store.saved["DAILY"].values()))
        self.assertEqual((row["delta"], row["state"]), (1.478, 655.724))
        self.assertNotIn("sum", row)

    async def test_first_sum_is_provider_delta_not_lifetime_meter(self):
        await self.import_frequency(
            Frequency.DAILY, [reading("2026-10-27T23:01:28+01:00", 655.724, 1.478)]
        )
        metadata, points, table = self.publications()[DAILY_ID]
        self.assertEqual((points[0]["sum"], points[0]["state"]), (1478, 655724))
        self.assertIs(table, recorder_statistics.Statistics)
        self.assertEqual(metadata["source"], DOMAIN)
        self.assertEqual(metadata["unit_class"], VolumeConverter.UNIT_CLASS)
        self.assertEqual(metadata["unit_of_measurement"], UnitOfVolume.LITERS)

    async def test_restart_replays_saved_ledger_without_baseline_drift(self):
        rows = [
            reading("2026-10-27T23:01:28+01:00", 655.724, 1.478),
            reading("2026-10-28T17:01:26+01:00", 656.48, 0.756),
        ]
        await self.import_frequency(Frequency.DAILY, rows)
        first = self.publications()[DAILY_ID][1]
        self.coordinator = make_coordinator(FakeStore(self.coordinator._store.saved))
        self.recorded.clear()
        self.queue.async_import_statistics.reset_mock()
        await self.coordinator._import_range(
            date(2026, 10, 27), date(2026, 10, 29), (Frequency.DAILY,)
        )
        self.assertEqual(self.publications()[DAILY_ID][1], first)
        self.assertEqual(first[-1]["sum"], 2234)
        self.assertEqual(self.coordinator._data[CONF_VALUE], 656.48)

    async def test_saved_data_recovers_before_failing_network_request(self):
        self.enterContext(patch.object(sensor.asyncio, "sleep", new_callable=AsyncMock))
        await self.import_frequency(
            Frequency.DAILY, [reading("2026-10-28T17:01:26+01:00", 656.48, 0.756)]
        )
        saved = deepcopy(self.coordinator._store.saved)
        self.coordinator = make_coordinator(FakeStore(saved))
        self.recorded.clear()
        self.queue.async_import_statistics.reset_mock()
        self.coordinator._api.consumptions.side_effect = AiguesApiServerError(
            "Provider unavailable"
        )
        with self.assertRaises(AiguesApiServerError):
            await self.coordinator._import_range(
                date(2026, 10, 27), date(2026, 10, 29), tuple(Frequency)
            )
        self.assertEqual(self.coordinator._data[CONF_VALUE], 656.48)
        self.assertEqual(self.publications()[DAILY_ID][1][-1]["sum"], 756)
        self.coordinator._store.async_save.assert_not_awaited()
        self.assertEqual(self.coordinator._store.saved, saved)

    async def test_missing_ledger_cannot_rebase_existing_statistics(self):
        self.last_statistics.return_value = {
            DAILY_ID: [{"start": 1793055600.0, "state": 655.724, "sum": 23.0}]
        }
        with self.assertRaisesRegex(ValueError, "ledger is missing"):
            await self.coordinator._import_range(
                date(2026, 10, 27), date(2026, 10, 29), tuple(Frequency)
            )
        self.coordinator._api.consumptions.assert_not_called()
        self.coordinator._store.async_save.assert_not_awaited()
        self.queue.async_import_statistics.assert_not_called()

    async def test_daily_and_hourly_never_mix_consumption(self):
        daily = [reading("2026-10-27T23:01:28+01:00", 655.724, 1.478)]
        hourly = [reading("2026-10-27T10:01:28+01:00", 655.0, 0.062)]
        self.coordinator._api.consumptions.side_effect = lambda *args, **kwargs: (
            daily if kwargs["frequency"] == "DAILY" else hourly
        )
        await self.coordinator._import_range(
            date(2026, 10, 27), date(2026, 10, 29), tuple(Frequency)
        )
        published = self.publications()
        self.assertEqual(set(published), {DAILY_ID, HOURLY_ID, ENERGY_ID})
        self.assertEqual([p["sum"] for p in published[DAILY_ID][1]], [1478])
        self.assertEqual([p["sum"] for p in published[HOURLY_ID][1]], [62])
        self.assertEqual(self.coordinator._data["imported_intervals"], {"daily": 1, "hourly": 1})

    async def test_overlapping_response_is_idempotent(self):
        rows = [reading("2026-10-27T10:01:28+01:00", 655.0, 0.062)]
        await self.import_frequency(Frequency.HOURLY, rows)
        first = deepcopy(self.coordinator._store.saved)
        self.queue.async_import_statistics.reset_mock()
        await self.import_frequency(Frequency.HOURLY, rows)
        self.assertEqual(self.coordinator._store.saved, first)
        self.queue.async_import_statistics.assert_not_called()
        self.assertEqual(len(first["HOURLY"]), 1)

    async def test_earlier_correction_recalculates_later_sums(self):
        rows = [
            reading("2026-10-27T10:01:28+01:00", 654.2, 0.1),
            reading("2026-10-27T11:01:28+01:00", 654.6, 0.2),
        ]
        await self.import_frequency(Frequency.HOURLY, rows)
        self.assertEqual([p["sum"] for p in self.publications()[HOURLY_ID][1]], [100, 300])
        self.queue.async_import_statistics.reset_mock()
        await self.import_frequency(
            Frequency.HOURLY, [reading("2026-10-27T10:01:28+01:00", 654.30, 0.15)]
        )
        self.assertEqual([p["sum"] for p in self.publications()[HOURLY_ID][1]], [150, 350])
        self.assertEqual(len(self.coordinator._store.saved["HOURLY"]), 2)

    async def test_invalid_partial_batch_persists_nothing(self):
        self.coordinator._api.consumptions.side_effect = [
            [reading("2026-10-27T23:01:28+01:00", 655.724, 1.478)],
            [{"datetime": "2026-10-28T01:00:00+01:00", "accumulatedConsumption": 656.0}],
        ]
        with self.assertRaises(ValueError):
            await self.coordinator._import_range(
                date(2026, 10, 27), date(2026, 10, 29), tuple(Frequency)
            )
        self.coordinator._store.async_save.assert_not_awaited()
        self.queue.async_import_statistics.assert_not_called()
        self.assertNotIn(CONF_VALUE, self.coordinator._data)

    async def test_later_api_error_does_not_commit_earlier_frequency(self):
        self.enterContext(patch.object(sensor.asyncio, "sleep", new_callable=AsyncMock))
        self.coordinator._api.consumptions.side_effect = [
            [reading("2026-10-27T23:01:28+01:00", 655.724, 1.478)]
        ] + [AiguesApiServerError("Provider error") for _ in range(5)]
        with self.assertRaises(AiguesApiServerError):
            await self.coordinator._import_range(
                date(2026, 10, 27), date(2026, 10, 29), tuple(Frequency)
            )
        self.coordinator._store.async_save.assert_not_awaited()
        self.queue.async_import_statistics.assert_not_called()

    async def test_corrupt_saved_ledger_is_not_published_or_replaced(self):
        self.coordinator._store = FakeStore(
            {"DAILY": {"broken": {"delta": float("nan")}}, "HOURLY": {}}
        )
        with self.assertRaises((ValueError, KeyError)):
            await self.coordinator._import_range(
                date(2026, 10, 27), date(2026, 10, 29), tuple(Frequency)
            )
        self.coordinator._store.async_save.assert_not_awaited()
        self.queue.async_import_statistics.assert_not_called()
        self.coordinator._api.consumptions.assert_not_called()

    async def test_committed_ledger_replays_after_recorder_failure(self):
        self.coordinator._api.consumptions.return_value = [
            reading("2026-10-27T23:01:28+01:00", 655.724, 1.478)
        ]
        self.queue.async_import_statistics.side_effect = RuntimeError("Recorder unavailable")
        with self.assertRaises(RuntimeError):
            await self.coordinator._import_range(
                date(2026, 10, 27), date(2026, 10, 29), (Frequency.DAILY,)
            )
        saved = deepcopy(self.coordinator._store.saved)
        self.assertEqual(len(saved["DAILY"]), 1)
        self.queue.async_import_statistics.side_effect = self.record_points
        self.coordinator = make_coordinator(FakeStore(saved))
        await self.coordinator._import_range(
            date(2026, 10, 27), date(2026, 10, 29), (Frequency.DAILY,)
        )
        self.assertEqual(self.publications()[DAILY_ID][1][-1]["sum"], 1478)
        self.assertEqual(self.coordinator._data[CONF_VALUE], 655.724)

    async def test_concurrent_imports_serialize_without_lost_intervals(self):
        entered, release = asyncio.Event(), asyncio.Event()
        active, maximum = 0, 0

        async def response(*args, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            if args[0] == date(2026, 10, 27):
                entered.set()
                await release.wait()
                rows = [reading("2026-10-27T23:01:28+01:00", 655.724, 1.478)]
            else:
                rows = [reading("2026-10-28T17:01:26+01:00", 656.48, 0.756)]
            active -= 1
            return rows

        self.coordinator._api.consumptions.side_effect = response
        first = asyncio.create_task(
            self.coordinator._import_range(
                date(2026, 10, 27), date(2026, 10, 28), (Frequency.DAILY,)
            )
        )
        await entered.wait()
        second = asyncio.create_task(
            self.coordinator._import_range(
                date(2026, 10, 28), date(2026, 10, 29), (Frequency.DAILY,)
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(self.coordinator._api.consumptions.call_count, 1)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(maximum, 1)
        self.assertEqual(len(self.coordinator._store.saved["DAILY"]), 2)
        self.assertEqual(self.publications()[DAILY_ID][1][-1]["sum"], 2234)
        self.coordinator._store.async_load.assert_awaited_once()


class RefreshAndManualImportTests(OfflineTests):
    async def test_empty_hourly_does_not_suppress_daily_or_display(self):
        daily = [reading("2026-10-28T17:01:26+01:00", 656.48, 0.756)]
        self.coordinator._api.consumptions.side_effect = lambda *args, **kwargs: (
            daily if kwargs["frequency"] == "DAILY" else []
        )
        result = await self.coordinator._async_update_data()
        self.assertEqual(result[CONF_VALUE], 656.48)
        self.assertEqual(set(self.publications()), {DAILY_ID, ENERGY_ID})
        self.assertEqual(result["imported_intervals"], {"daily": 1, "hourly": 0})

    async def test_hourly_refresh_queries_past_seven_days_and_today_separately(self):
        await self.coordinator._async_update_data()
        hourly = [
            c
            for c in self.coordinator._api.consumptions.call_args_list
            if c.kwargs["frequency"] == "HOURLY"
        ]
        self.assertEqual(
            hourly,
            [
                call(date(2026, 10, 22), date(2026, 10, 29), CONTRACT, frequency="HOURLY"),
                call(date(2026, 10, 29), date(2026, 10, 30), CONTRACT, frequency="HOURLY"),
            ],
        )
        self.assertEqual(
            self.coordinator._ensure_token.await_count,
            self.coordinator._api.consumptions.call_count,
        )

    async def test_refresh_failure_surfaces_updatefailed(self):
        self.enterContext(patch.object(sensor.asyncio, "sleep", new_callable=AsyncMock))
        self.coordinator._api.consumptions.side_effect = AiguesApiServerError(
            "Provider unavailable"
        )
        with self.assertRaises(UpdateFailed):
            await self.coordinator._async_update_data()
        self.coordinator._store.async_save.assert_not_awaited()
        self.queue.async_import_statistics.assert_not_called()

    async def test_auth_failure_remains_configentry_auth_failure(self):
        self.coordinator._api.consumptions.side_effect = AiguesApiAuthError("Invalid JWT")
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.coordinator._async_update_data()

    async def test_manual_import_updates_display_and_clears_flag_once_successful(self):
        self.coordinator._should_import_history = True
        entry = self.coordinator.hass.config_entries.async_get_entry.return_value
        entry.options = {CONF_SHOULD_IMPORT_HISTORY: True, CONF_HISTORY_DAYS: 365}
        self.coordinator._api.consumptions.return_value = [
            reading("2026-10-28T17:01:26+01:00", 656.48, 0.756)
        ]
        await self.coordinator.import_old_consumptions(days=2, frequency="DAILY")
        self.assertEqual(self.coordinator._data[CONF_VALUE], 656.48)
        self.assertEqual(self.coordinator._data[CONF_STATE], "2026-10-28T17:01:26+01:00")
        self.assertFalse(self.coordinator._should_import_history)
        self.assertFalse(entry.options[CONF_SHOULD_IMPORT_HISTORY])
        self.assertEqual(entry.options[CONF_HISTORY_DAYS], 365)
        self.assertEqual(self.coordinator.async_set_updated_data.call_count, 2)
        self.coordinator.async_set_updated_data.assert_called_with(self.coordinator._data)
        await self.coordinator.import_old_consumptions(days=2, frequency="DAILY")
        self.coordinator.hass.config_entries.async_update_entry.assert_called_once()
        self.assertTrue(
            all(
                c.kwargs["frequency"] == "DAILY"
                for c in self.coordinator._api.consumptions.call_args_list
            )
        )

    async def test_failed_manual_import_retains_pending_flag(self):
        self.enterContext(patch.object(sensor.asyncio, "sleep", new_callable=AsyncMock))
        self.coordinator._should_import_history = True
        self.coordinator._api.consumptions.side_effect = AiguesApiServerError(
            "Provider unavailable"
        )
        with self.assertRaises(AiguesApiServerError):
            await self.coordinator.import_old_consumptions(days=2)
        self.assertTrue(self.coordinator._should_import_history)
        self.coordinator.hass.config_entries.async_update_entry.assert_not_called()
        self.coordinator.async_set_updated_data.assert_called_once_with(self.coordinator._data)
        self.assertEqual(self.coordinator._data["import_status"]["phase"], "failed")


class IdentityAndIsolationTests(OfflineTests):
    async def test_constructor_scopes_state_store_and_two_external_ids(self):
        old_store = {CONTRACT: {"sentinel": object()}}
        hass = SimpleNamespace(
            data={"aigues_barcelona": old_store},
            config_entries=SimpleNamespace(async_get_entry=Mock(return_value=None)),
        )
        with (
            patch.object(sensor.DataUpdateCoordinator, "__init__", return_value=None),
            patch.object(sensor, "Store", return_value=FakeStore()) as storage,
        ):
            coordinator = sensor.AiguesHistoryCoordinator(hass, self.coordinator._entry(), CONTRACT)
        self.assertEqual(coordinator.sensor_entity_id, f"sensor.{DOMAIN}_{OBJECT_ID}")
        self.assertEqual(coordinator.sensor_unique_id, f"{DOMAIN}_{OBJECT_ID}_meter")
        self.assertEqual(
            coordinator.statistic_ids, {Frequency.DAILY: DAILY_ID, Frequency.HOURLY: HOURLY_ID}
        )
        self.assertEqual(storage.call_args.args[2], f"{DOMAIN}.history-entry.{OBJECT_ID}")
        self.assertIs(hass.data["aigues_barcelona"], old_store)
        self.assertIs(hass.data[DOMAIN][CONTRACT]["coordinator"], coordinator)

    async def test_contracts_share_only_history_domain_token_lock(self):
        old_lock = asyncio.Lock()
        hass = SimpleNamespace(
            data={"aigues_barcelona": {"_token_lock": old_lock}},
            config_entries=SimpleNamespace(async_get_entry=Mock(return_value=None)),
        )
        with (
            patch.object(sensor.DataUpdateCoordinator, "__init__", return_value=None),
            patch.object(sensor, "Store", side_effect=lambda *args: FakeStore()),
        ):
            first = sensor.AiguesHistoryCoordinator(hass, self.coordinator._entry(), "FIRST")
            second = sensor.AiguesHistoryCoordinator(hass, self.coordinator._entry(), "SECOND")
        self.assertIs(first._token_lock, second._token_lock)
        self.assertIs(first._token_lock, hass.data[DOMAIN]["_token_lock"])
        self.assertIsNot(first._token_lock, old_lock)
        self.assertIs(first._request_gate, second._request_gate)
        self.assertIs(first._request_gate, hass.data[DOMAIN]["_request_gate"])

    async def test_first_refreshes_are_sequential_within_one_background_task(self):
        entered, release = asyncio.Event(), asyncio.Event()
        order, background = [], []

        async def first_refresh():
            order.append("first-start")
            entered.set()
            await release.wait()
            order.append("first-end")

        async def second_refresh():
            order.extend(("second-start", "second-end"))

        first = SimpleNamespace(async_refresh=AsyncMock(side_effect=first_refresh))
        second = SimpleNamespace(async_refresh=AsyncMock(side_effect=second_refresh))
        entry = SimpleNamespace(
            entry_id="history-entry",
            data={
                "username": "unused",
                "password": "unused",
                CONF_CONTRACT: ["FIRST", "SECOND", "FIRST", "first"],
            },
            options={CONF_SHOULD_IMPORT_HISTORY: False},
            runtime_data=(first, second),
            async_create_background_task=Mock(
                side_effect=lambda hass, coroutine, name: background.append(coroutine)
            ),
            async_on_unload=Mock(),
        )
        hass = SimpleNamespace(
            data={},
            state=CoreState.running,
            is_running=True,
            async_run_hass_job=lambda job, *args: job.target(*args),
        )
        with (
            patch.object(sensor, "AiguesHistoryCoordinator", side_effect=[first, second]),
            patch.object(
                sensor, "AiguesHistoryMeter", side_effect=lambda c: SimpleNamespace(coordinator=c)
            ),
        ):
            await sensor.async_setup_entry(hass, entry, Mock())
        self.assertEqual(len(background), 1)
        task = asyncio.create_task(background[0])
        await entered.wait()
        second.async_refresh.assert_not_awaited()
        release.set()
        await task
        self.assertEqual(order, ["first-start", "first-end", "second-start", "second-end"])

    async def test_last_unload_removes_shared_lock_but_other_entry_keeps_it(self):
        old_store, shared_lock = {"sentinel": object()}, asyncio.Lock()
        other = SimpleNamespace(entry_id="other-entry")
        hass = SimpleNamespace(
            data={
                "aigues_barcelona": old_store,
                DOMAIN: {
                    "_token_lock": shared_lock,
                    CONTRACT: {"coordinator": self.coordinator},
                    "OTHER": {"coordinator": other},
                },
            },
            config_entries=SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True)),
        )
        await history_integration.async_unload_entry(
            hass, SimpleNamespace(entry_id="history-entry")
        )
        self.assertIs(hass.data[DOMAIN]["_token_lock"], shared_lock)
        self.assertIs(hass.data[DOMAIN]["OTHER"]["coordinator"], other)
        await history_integration.async_unload_entry(hass, SimpleNamespace(entry_id="other-entry"))
        self.assertNotIn(DOMAIN, hass.data)
        self.assertIs(hass.data["aigues_barcelona"], old_store)

    async def test_display_sensor_has_no_competing_native_statistics(self):
        meter = sensor.AiguesHistoryMeter(self.coordinator)
        self.assertEqual(meter.entity_id, f"sensor.{DOMAIN}_{OBJECT_ID}")
        self.assertEqual(meter.unique_id, f"{DOMAIN}_{OBJECT_ID}_meter")
        self.assertEqual(meter.name, "Meter reading")
        self.assertIsNone(meter.state_class)
        old_id = f"sensor.contador_{OBJECT_ID}"
        states = [
            State(meter.entity_id, "656.48", {"state_class": meter.state_class}),
            State(old_id, "651.58", {"state_class": SensorStateClass.TOTAL}),
        ]
        hass = SimpleNamespace(states=SimpleNamespace(all=lambda domain: states))
        recorder = SimpleNamespace(entity_filter=lambda entity_id: True)
        with patch.object(sensor_recorder, "get_instance", return_value=recorder):
            self.assertEqual(
                [s.entity_id for s in sensor_recorder._get_sensor_states(hass)], [old_id]
            )

    async def test_real_water_energy_validator_accepts_both_owned_series(self):
        await self.import_frequency(
            Frequency.DAILY, [reading("2026-10-28T17:01:26+01:00", 656.48, 0.756)]
        )
        await self.import_frequency(
            Frequency.HOURLY, [reading("2026-10-28T17:01:26+01:00", 656.48, 0.062)]
        )
        for statistic_id, (metadata, _, _) in self.publications().items():
            issues, hass = Mock(), SimpleNamespace(states=Mock())
            energy_validate._async_validate_usage_stat(
                hass,
                {statistic_id: (123, metadata)},
                statistic_id,
                energy_validate.WATER_USAGE_DEVICE_CLASSES,
                energy_validate.WATER_USAGE_UNITS,
                energy_validate.WATER_UNIT_ERROR,
                issues,
            )
            issues.add_issue.assert_not_called()
            hass.states.get.assert_not_called()

    async def test_unload_preserves_original_component_state(self):
        old_store = {CONTRACT: {"coordinator": object()}}
        hass = SimpleNamespace(
            data={
                "aigues_barcelona": old_store,
                DOMAIN: {CONTRACT: {"coordinator": self.coordinator}},
            },
            config_entries=SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True)),
        )
        result = await history_integration.async_unload_entry(
            hass, SimpleNamespace(entry_id="history-entry")
        )
        self.assertTrue(result)
        self.assertNotIn(DOMAIN, hass.data)
        self.assertIs(hass.data["aigues_barcelona"], old_store)

    async def test_config_flow_account_and_import_default_are_isolated(self):
        lookup = Mock(return_value=None)
        flow = config_flow.AiguesBarcelonaHistoryConfigFlow()
        flow.hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_entry_for_domain_unique_id=lookup, async_entries=Mock(return_value=[])
            )
        )
        flow.handler, flow.flow_id, flow.context = (
            DOMAIN,
            "offline-history-flow",
            {"source": "user"},
        )
        flow._async_in_progress = Mock(return_value=[])
        credentials = {
            "username": "X1234567L",
            "password": "unused",
            "twocaptcha_api_key": "unused",
        }
        with patch.object(
            config_flow,
            "validate_credentials",
            AsyncMock(return_value={CONF_CONTRACT: [CONTRACT], "token": "unused"}),
        ):
            result = await flow.async_step_user(credentials)
            self.assertEqual(result["step_id"], "import_settings")
            result = await flow.async_step_import_settings({CONF_SHOULD_IMPORT_HISTORY: False})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(flow.unique_id, f"{DOMAIN}_X1234567L")
        self.assertTrue(result["title"].startswith(NAME))
        self.assertIs(result["options"][CONF_SHOULD_IMPORT_HISTORY], False)
        self.assertTrue(all(c.args[0] == DOMAIN for c in lookup.call_args_list))

    async def test_first_setup_does_not_start_bulk_history_synchronously(self):
        entry = SimpleNamespace(
            entry_id="history-entry",
            data={"username": "unused", "password": "unused", CONF_CONTRACT: [CONTRACT]},
            options={CONF_SHOULD_IMPORT_HISTORY: False},
            async_on_unload=Mock(),
        )
        coordinator = SimpleNamespace(async_refresh=AsyncMock())
        entry.runtime_data = (coordinator,)
        hass = SimpleNamespace(
            data={},
            state=CoreState.not_running,
            is_running=False,
            bus=SimpleNamespace(async_listen_once=Mock()),
        )
        with (
            patch.object(sensor, "AiguesHistoryCoordinator") as coordinator_class,
            patch.object(
                sensor, "AiguesHistoryMeter", side_effect=lambda c: SimpleNamespace(coordinator=c)
            ),
        ):
            await sensor.async_setup_entry(hass, entry, Mock())
        coordinator_class.assert_not_called()
        hass.bus.async_listen_once.assert_called_once()
        coordinator.async_refresh.assert_not_called()

    async def test_old_and_new_services_stay_in_their_domains(self):
        handlers = {}
        registry = SimpleNamespace(
            has_service=lambda domain, name: (domain, name) in handlers,
            async_register=lambda domain, name, callback, **kwargs: handlers.__setitem__(
                (domain, name), callback
            ),
        )
        old = SimpleNamespace(contract=CONTRACT, import_old_consumptions=AsyncMock())
        new = SimpleNamespace(
            contract=CONTRACT,
            entry_id="history-entry",
            import_old_consumptions=AsyncMock(),
            _set_manual_import_reserved=Mock(),
        )
        entry = SimpleNamespace(
            async_create_background_task=lambda hass, coroutine, name: asyncio.create_task(
                coroutine
            )
        )
        hass = SimpleNamespace(
            services=registry,
            config_entries=SimpleNamespace(async_get_entry=Mock(return_value=entry)),
            data={
                "aigues_barcelona": {CONTRACT: {"coordinator": old}},
                DOMAIN: {CONTRACT: {"coordinator": new}},
            },
        )
        old_handler = AsyncMock()
        handlers[("aigues_barcelona", "import_historical_data")] = old_handler
        await service.async_setup(hass, {})
        request = SimpleNamespace(data={CONF_CONTRACT: CONTRACT, CONF_HISTORY_DAYS: 7})
        await handlers[(DOMAIN, "import_historical_data")](request)
        new.import_old_consumptions.assert_awaited_once_with(days=7, frequency="ALL")
        old.import_old_consumptions.assert_not_awaited()
        self.assertIs(handlers[("aigues_barcelona", "import_historical_data")], old_handler)
        old_handler.assert_not_awaited()


class ProviderCompatibilityTests(OfflineTests):
    async def test_provider_query_protocol_for_both_frequencies(self):
        response = Mock()
        response.json.return_value = {"data": []}
        new = AiguesBarcelonaHistoryApiClient("unused", "unused", "unused", session=Mock())
        for frequency in Frequency:
            new._query = Mock(return_value=response)
            result = new.consumptions(
                date(2026, 10, 17),
                date(2026, 10, 24),
                CONTRACT,
                "test-user",
                frequency=frequency.value,
            )
            self.assertEqual(result, [])
            new._query.assert_called_once_with(
                "/ofex-water-consumptions-api/meter/consumptions",
                {
                    "consumptionFrequency": frequency.value,
                    "contractNumber": CONTRACT,
                    "clientId": "test-user",
                    "userId": "test-user",
                    "lang": "ca",
                    "fromDate": "17-10-2026",
                    "toDate": "24-10-2026",
                    "showNegativeValues": "false",
                },
            )
        self.assertTrue(new.headers["User-Agent"].startswith("hass-aigues-barcelona-history/"))

    async def test_service_schema_defaults_and_frequency_choices(self):
        self.assertEqual(service.SERVICE_SCHEMA({})["frequency"], "ALL")
        for frequency in ("ALL", "DAILY", "HOURLY"):
            self.assertEqual(
                service.SERVICE_SCHEMA({"frequency": frequency})["frequency"], frequency
            )
        with self.assertRaises(service.vol.Invalid):
            service.SERVICE_SCHEMA({"frequency": "MINUTELY"})

    async def test_manifest_and_service_documentation(self):
        manifest = json.loads(Path(sensor.__file__).with_name("manifest.json").read_text())
        self.assertEqual((manifest["domain"], manifest["name"]), (DOMAIN, NAME))
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+$")
        definitions = yaml.safe_load(Path(service.__file__).with_name("services.yaml").read_text())
        self.assertEqual(
            set(definitions),
            {"import_historical_data", "reset_and_refresh_data", "import_hourly_date_range"},
        )
        for name, definition in definitions.items():
            self.assertTrue(definition["name"].startswith(f"{NAME}: "))
            expected_fields = (
                {"contract", "start_date", "end_date"}
                if name == "import_hourly_date_range"
                else {"contract", "history_days", "frequency"}
            )
            self.assertEqual(set(definition["fields"]), expected_fields)


if __name__ == "__main__":
    unittest.main()
