"""Display the meter and publish independently owned provider statistics."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from functools import partial
import logging
import math
import time

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    get_metadata,
)
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_STATE,
    CONF_TOKEN,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_START,
    UnitOfVolume,
)
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter
from requests.exceptions import ChunkedEncodingError, ConnectionError, Timeout
from voluptuous import Invalid

from . import canonical_contracts
from .api import (
    AiguesApiAuthError,
    AiguesApiRateLimitError,
    AiguesApiServerError,
    AiguesApiSessionLimitError,
    AiguesBarcelonaHistoryApiClient,
)
from .const import (
    ATTR_LAST_MEASURE,
    CONF_2CAPTCHA_APIKEY,
    CONF_AUTH_SESSION_LIMIT,
    CONF_HISTORY_DAYS,
    CONF_HOURLY_HISTORY_DAYS,
    CONF_RETRY_NOT_BEFORE,
    CONF_SCAN_PERIOD,
    CONF_SHOULD_IMPORT_HISTORY,
    CONF_VALUE,
    DEFAULT_HISTORY_DAYS,
    DOMAIN,
    NAME,
)
from .settings import effective_settings, validate_history_days
from .statistics import (
    Frequency,
    build_statistics,
    describe_hourly_coverage,
    normalize_readings,
    plan_windows,
    reconcile_intervals,
    validate_saved_ledger,
)

_LOGGER = logging.getLogger(__name__)
MAX_FETCH_ATTEMPTS = 5
MAX_INLINE_RETRY_SECONDS = 60
# The API's query-date labels and supplied timestamps use fixed CET year round.
PROVIDER_TIME_ZONE = timezone(timedelta(hours=1))


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Create independent meters without blocking startup on historical imports."""
    meters = [AiguesHistoryMeter(coordinator) for coordinator in entry.runtime_data]
    async_add_entities(meters)

    @callback
    def first_refresh(*_):
        async def refresh_meters():
            for meter in meters:
                await meter.coordinator.async_refresh()

        entry.async_create_background_task(hass, refresh_meters(), f"{DOMAIN} first refresh")

    if hass.state == CoreState.running:
        first_refresh()
    else:
        entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, first_refresh))


class AiguesHistoryCoordinator(DataUpdateCoordinator):
    """Keep source intervals durable and verify their recorder publication."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry,
        contract: str,
    ) -> None:
        settings = effective_settings(entry)
        self.contract = canonical_contracts([contract])[0]
        self.id = self.contract.lower()
        self.sensor_entity_id = f"sensor.{DOMAIN}_{self.id}"
        self.sensor_unique_id = f"{DOMAIN}_{self.id}_meter"
        self.statistic_ids = {
            Frequency.DAILY: f"{DOMAIN}:{self.id}_consumption",
            Frequency.HOURLY: f"{DOMAIN}:{self.id}_hourly_consumption",
        }
        self.entry_id = entry.entry_id
        self._should_import_history = settings[CONF_SHOULD_IMPORT_HISTORY]
        self._lock = asyncio.Lock()
        domain_data = hass.data.setdefault(DOMAIN, {})
        self._token_lock = domain_data.setdefault("_token_lock", asyncio.Lock())
        self._store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.{self.id}")
        self._ledger = None
        self._stopping = False
        self._data = domain_data.setdefault(self.contract, {})
        self._data["coordinator"] = self
        self._api = AiguesBarcelonaHistoryApiClient(
            entry.data[CONF_USERNAME],
            entry.data.get(CONF_PASSWORD, ""),
            entry.data.get(CONF_2CAPTCHA_APIKEY, ""),
            contract,
        )
        if token := entry.data.get(CONF_TOKEN):
            self._api.set_token(token)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{self.id}",
            update_interval=timedelta(seconds=settings[CONF_SCAN_PERIOD]),
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Keep the meter and its import buttons on one History-only device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.id)},
            name=f"{NAME} {self.contract}",
            manufacturer="Aigües de Barcelona",
        )

    def _assert_active(self) -> None:
        if self._stopping or getattr(self, "_shutdown_requested", False):
            raise asyncio.CancelledError

    async def async_shutdown(self) -> None:
        """Reject new work and let entry ownership cancel pending imports."""
        self._stopping = True
        await super().async_shutdown()

    def _entry(self):
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if entry is None:
            raise ConfigEntryAuthFailed("History config entry not found")
        return entry

    def _update_entry_data(self, **changes) -> None:
        self._assert_active()
        entry = self._entry()
        data = {**entry.data, **changes}
        if data != entry.data:
            self.hass.config_entries.async_update_entry(entry, data=data)

    def _session_limit_failure(self) -> ConfigEntryAuthFailed:
        self._update_entry_data(**{CONF_AUTH_SESSION_LIMIT: True})
        return ConfigEntryAuthFailed(
            "Aigües session limit requires explicit authentication recovery"
        )

    async def _ensure_token(self) -> None:
        """Persist known session limits so automatic runs cannot buy more logins."""
        async with self._token_lock:
            self._assert_active()
            entry = self._entry()
            if entry.data.get(CONF_AUTH_SESSION_LIMIT, False):
                raise self._session_limit_failure()
            if not self._api.is_token_expired():
                return
            if stored := entry.data.get(CONF_TOKEN):
                self._api.set_token(stored)
                if not self._api.is_token_expired():
                    return
            if not entry.data.get(CONF_PASSWORD) or not entry.data.get(CONF_2CAPTCHA_APIKEY):
                raise ConfigEntryAuthFailed("Provide a current token or sign-in credentials")
            try:
                login_ok = await self.hass.async_add_executor_job(self._api.login)
            except AiguesApiSessionLimitError as err:
                raise self._session_limit_failure() from err
            self._assert_active()
            token = self._api.get_token()
            if not login_ok or not token:
                raise RuntimeError("Aigües login did not return a token")
            self._update_entry_data(**{CONF_TOKEN: token})

    async def _async_update_data(self):
        try:
            if self._should_import_history:
                await self.import_configured_consumptions(clear_startup_request=True)
            else:
                today = dt_util.now().date()
                await self._import_range(
                    today - timedelta(days=7),
                    today + timedelta(days=1),
                    tuple(Frequency),
                )
        except AiguesApiAuthError as err:
            raise ConfigEntryAuthFailed from err
        except ConfigEntryAuthFailed:
            raise
        except UpdateFailed:
            raise
        except Exception as err:
            raise UpdateFailed(f"History import failed ({type(err).__name__})") from None
        return self._data

    def _prepare_statistics(self, ledger):
        """Validate both frequencies and aggregate sums before any persistent write."""
        canonical = {}
        statistics = {}
        for frequency in Frequency:
            canonical[frequency.value] = validate_saved_ledger(
                ledger[frequency.value], frequency, dt_util.DEFAULT_TIME_ZONE
            )
            statistics[frequency] = build_statistics(canonical[frequency.value])
        return canonical, statistics

    async def _load_ledger(self) -> None:
        if self._ledger is None:
            saved = await self._store.async_load()
            if saved is None:
                saved = {frequency.value: {} for frequency in Frequency}
            self._ledger, _ = self._prepare_statistics(saved)

    async def _wait_for_request(self, backoff: float) -> None:
        while True:
            self._assert_active()
            deadline = self._entry().data.get(CONF_RETRY_NOT_BEFORE, 0)
            if (
                isinstance(deadline, bool)
                or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline)
            ):
                raise ValueError("Invalid stored provider retry deadline")
            delay = max(backoff, deadline - time.time())
            if delay > MAX_INLINE_RETRY_SECONDS:
                raise AiguesApiRateLimitError(
                    "Provider retry pause is still active", retry_after=delay
                )
            if delay <= 0:
                return
            await asyncio.sleep(delay)
            backoff = 0

    async def _request_consumptions(self, start: date, end: date, frequency: Frequency) -> list:
        """Retry transient GET errors while honoring provider-requested pauses."""
        request = partial(
            self._api.consumptions,
            start,
            end,
            self.contract,
            frequency=frequency.value,
        )
        for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
            await self._wait_for_request(0 if attempt == 1 else 2 ** (attempt - 2))
            try:
                rows = await self.hass.async_add_executor_job(request)
                self._assert_active()
                deadline = self._entry().data.get(CONF_RETRY_NOT_BEFORE, 0)
                if deadline and deadline <= time.time():
                    self._update_entry_data(**{CONF_RETRY_NOT_BEFORE: 0})
                return rows
            except AiguesApiSessionLimitError as err:
                raise self._session_limit_failure() from err
            except (
                Timeout,
                ConnectionError,
                ChunkedEncodingError,
                AiguesApiServerError,
                AiguesApiRateLimitError,
            ) as err:
                self._assert_active()
                retry_after = getattr(err, "retry_after", None)
                if retry_after is not None:
                    deadline = max(
                        self._entry().data.get(CONF_RETRY_NOT_BEFORE, 0),
                        time.time() + retry_after,
                    )
                    self._update_entry_data(**{CONF_RETRY_NOT_BEFORE: deadline})
                if attempt == MAX_FETCH_ATTEMPTS:
                    raise
                _LOGGER.warning(
                    "%s consumption request for %s (%s to %s) failed with %s; attempt %d/%d",
                    frequency.value,
                    self.contract,
                    start,
                    end,
                    type(err).__name__,
                    attempt,
                    MAX_FETCH_ATTEMPTS,
                )

    async def _fetch(self, start: date, end: date, frequency: Frequency) -> dict:
        result = {}
        if start >= end:
            plan_windows(start, end, frequency)  # Validate reversed or invalid bounds.
            return result
        # The provider labels hours in fixed CET. Madrid's summer midnight is
        # in the preceding provider date. Fetch that boundary, then filter by
        # the requested HA dates. Keep today's unreliable API response separate.
        query_begin = start - timedelta(days=1) if frequency is Frequency.HOURLY else start
        provider_today = dt_util.now().astimezone(PROVIDER_TIME_ZONE).date()
        query_stop = min(end, provider_today + timedelta(days=1))
        if query_begin >= query_stop:
            return result
        windows = plan_windows(
            query_begin,
            query_stop,
            frequency,
            split_at=provider_today,
        )
        for query_start, query_end in windows:
            rows = await self._request_consumptions(query_start, query_end, frequency)
            if not isinstance(rows, list):
                raise ValueError(f"Invalid {frequency.value} response for {query_start}")
            response_stop = (
                query_end + timedelta(days=1) if frequency is Frequency.DAILY else query_end
            )
            incoming = normalize_readings(
                rows,
                frequency,
                dt_util.DEFAULT_TIME_ZONE,
                startdate=start if frequency is Frequency.HOURLY else query_start,
                enddate=end if frequency is Frequency.HOURLY else response_stop,
            )
            result = reconcile_intervals(
                result,
                incoming,
                frequency,
                dt_util.DEFAULT_TIME_ZONE,
                startdate=start,
                enddate=end,
            )
        return result

    async def _import_range(self, start: date, end: date, frequencies: tuple) -> None:
        await self._import_ranges({frequency: (start, end) for frequency in frequencies})

    async def _import_ranges(self, ranges: dict) -> None:
        """Commit different hourly/reference depths as one validated batch."""
        async with self._lock:
            self._assert_active()
            await self._load_ledger()
            await self._publish()
            self._update_display()
            await self._wait_for_request(0)
            await self._ensure_token()
            candidate = dict(self._ledger)
            for frequency, (start, end) in ranges.items():
                incoming = await self._fetch(start, end, frequency)
                candidate[frequency.value] = reconcile_intervals(
                    candidate[frequency.value],
                    incoming,
                    frequency,
                    dt_util.DEFAULT_TIME_ZONE,
                    startdate=start,
                    enddate=end,
                )
            candidate, _ = self._prepare_statistics(candidate)
            self._assert_active()
            await self._store.async_save(candidate)
            self._ledger = candidate
            await self._publish()
            self._update_display()

    async def _statistics_snapshot(self, frequency, points):
        statistic_id = self.statistic_ids[frequency]
        rows = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            len(points) + 1,
            statistic_id,
            False,
            {"state", "sum"},
        )
        return {
            row["start"]: (row.get("state"), row.get("sum")) for row in rows.get(statistic_id, [])
        }

    @staticmethod
    def _snapshot_matches(actual, expected) -> bool:
        return actual.keys() == expected.keys() and all(
            stored is not None and math.isclose(stored, wanted, rel_tol=0, abs_tol=1e-9)
            for timestamp, values in expected.items()
            for stored, wanted in zip(actual[timestamp], values)
        )

    def _statistic_metadata(self, frequency):
        """Keep source ownership and human-readable series roles in one place."""
        role = (
            "Provider daily totals (reference)"
            if frequency is Frequency.DAILY
            else "Water consumption (reported hours)"
        )
        return {
            "mean_type": 0,
            "unit_class": VolumeConverter.UNIT_CLASS,
            "has_sum": True,
            "name": f"{NAME} {self.id}: {role}",
            "source": DOMAIN,
            "statistic_id": self.statistic_ids[frequency],
            "unit_of_measurement": UnitOfVolume.CUBIC_METERS,
        }

    async def _stored_metadata(self):
        return await get_instance(self.hass).async_add_executor_job(
            partial(get_metadata, self.hass, statistic_ids=set(self.statistic_ids.values()))
        )

    async def _publish(self) -> None:
        """Trust recorder readback, not successful queueing or an in-memory cache."""
        self._assert_active()
        _, statistics = self._prepare_statistics(self._ledger)
        stored_metadata = await self._stored_metadata()
        metadata = {frequency: self._statistic_metadata(frequency) for frequency in Frequency}
        expected = {
            frequency: {
                point["start"].timestamp(): (point["state"], point["sum"]) for point in points
            }
            for frequency, points in statistics.items()
        }
        changed = []
        # Check coverage for both series before enqueueing either one.
        for frequency, points in statistics.items():
            actual = await self._statistics_snapshot(frequency, points)
            if not actual.keys() <= expected[frequency].keys():
                raise ValueError(
                    "History interval ledger is missing recorded intervals; "
                    "restore its backup before importing"
                )
            stored = stored_metadata.get(self.statistic_ids[frequency])
            name_changed = stored is None or stored[1]["name"] != metadata[frequency]["name"]
            if not self._snapshot_matches(actual, expected[frequency]) or (points and name_changed):
                changed.append(frequency)
        for frequency in changed:
            self._assert_active()
            async_add_external_statistics(self.hass, metadata[frequency], statistics[frequency])
        if changed:
            await get_instance(self.hass).async_block_till_done()
            stored_metadata = await self._stored_metadata()
            for frequency in changed:
                actual = await self._statistics_snapshot(frequency, statistics[frequency])
                if not self._snapshot_matches(actual, expected[frequency]):
                    raise UpdateFailed("Recorder did not persist the complete History batch")
                stored = stored_metadata.get(self.statistic_ids[frequency])
                if stored is None or stored[1]["name"] != metadata[frequency]["name"]:
                    raise UpdateFailed("Recorder did not persist the History series name")

    def _update_display(self) -> None:
        readings = [row for ledger in self._ledger.values() for row in ledger.values()]
        if readings:
            latest = max(readings, key=lambda row: datetime.fromisoformat(row["observed_at"]))
            self._data[CONF_VALUE] = latest["state"]
            self._data[CONF_STATE] = latest["observed_at"]
        self._data["statistics"] = {
            "daily": self.statistic_ids[Frequency.HOURLY],
            "hourly": self.statistic_ids[Frequency.HOURLY],
            "provider_daily": self.statistic_ids[Frequency.DAILY],
        }
        self._data["imported_intervals"] = {
            frequency.value.lower(): len(self._ledger[frequency.value]) for frequency in Frequency
        }
        self._data["hourly_coverage"] = describe_hourly_coverage(
            self._ledger[Frequency.HOURLY.value], dt_util.DEFAULT_TIME_ZONE
        )

    async def import_old_consumptions(
        self, days: int = DEFAULT_HISTORY_DAYS, frequency: str = "ALL"
    ):
        try:
            days = validate_history_days(days)
        except Invalid as error:
            raise ValueError(str(error)) from error
        frequencies = tuple(Frequency) if frequency == "ALL" else (Frequency(frequency),)
        await self._import_days({item: days for item in frequencies}, clear_startup_request=True)

    async def import_configured_consumptions(
        self, frequency: str = "ALL", *, clear_startup_request: bool = False
    ) -> None:
        """Use each history's configured depth without overwriting the other."""
        settings = effective_settings(self._entry())
        days = {
            Frequency.HOURLY: settings[CONF_HOURLY_HISTORY_DAYS],
            Frequency.DAILY: settings[CONF_HISTORY_DAYS],
        }
        if frequency != "ALL":
            selected = Frequency(frequency)
            days = {selected: days[selected]}
        await self._import_days(days, clear_startup_request=clear_startup_request)

    async def async_import_configured_history(self, frequency: str) -> None:
        """Run a button import under the same entry ownership as services."""
        from .service import async_run_import

        await async_run_import(self.hass, self, frequency=frequency)

    async def _import_days(self, days: dict, *, clear_startup_request: bool) -> None:
        today = dt_util.now().date()
        await self._import_ranges(
            {
                frequency: (today - timedelta(days=count), today + timedelta(days=1))
                for frequency, count in days.items()
            }
        )
        self._assert_active()
        if clear_startup_request:
            self._should_import_history = False
            entry = self._entry()
            coordinators = getattr(entry, "runtime_data", (self,))
            if (
                not any(item._should_import_history for item in coordinators)
                and effective_settings(entry)[CONF_SHOULD_IMPORT_HISTORY]
            ):
                self.hass.config_entries.async_update_entry(
                    entry, options={**entry.options, CONF_SHOULD_IMPORT_HISTORY: False}
                )
        self.async_set_updated_data(self._data)


class AiguesHistoryMeter(CoordinatorEntity, SensorEntity):
    """Display the physical meter without automatic recorder statistics."""

    _attr_icon = "mdi:water-pump"
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = None
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self.entity_id = coordinator.sensor_entity_id
        self._attr_name = "Meter reading"
        self._attr_unique_id = coordinator.sensor_unique_id

    @property
    def native_value(self):
        return self.coordinator._data.get(CONF_VALUE)

    @property
    def device_info(self):
        return self.coordinator.device_info

    @property
    def extra_state_attributes(self):
        return {
            ATTR_LAST_MEASURE: self.coordinator._data.get(CONF_STATE),
            "statistics": self.coordinator._data.get("statistics", {}),
            "imported_intervals": self.coordinator._data.get("imported_intervals", {}),
            "hourly_coverage": self.coordinator._data.get("hourly_coverage", {}),
        }
