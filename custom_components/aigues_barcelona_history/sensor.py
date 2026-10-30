"""Display the meter and publish independently owned provider statistics."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from functools import partial
import hashlib
import json
import logging
import math
import time
from time import monotonic

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
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.start import async_at_start
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter
from requests.exceptions import ChunkedEncodingError, ConnectionError, RequestException, Timeout
from voluptuous import Invalid

from . import canonical_contracts
from .api import (
    AiguesApiAuthError,
    AiguesApiError,
    AiguesApiRateLimitError,
    AiguesApiServerError,
    AiguesApiSessionLimitError,
    AiguesBarcelonaHistoryApiClient,
)
from .backfill import (
    MAX_HOURLY_BACKFILL_REQUESTS,
    REGULAR_HOURLY_LOOKBACK_DAYS,
    plan_hourly_backfill,
    validate_hourly_backfill_attempts,
)
from .const import (
    ATTR_LAST_MEASURE,
    CONF_2CAPTCHA_APIKEY,
    CONF_AUTH_SESSION_LIMIT,
    CONF_CONTRACT,
    CONF_ENERGY_DAILY_FALLBACK,
    CONF_HISTORY_DAYS,
    CONF_HOURLY_HISTORY_DAYS,
    CONF_RETRY_NOT_BEFORE,
    CONF_SCAN_PERIOD,
    CONF_SHOULD_IMPORT_HISTORY,
    CONF_VALUE,
    DEFAULT_HISTORY_DAYS,
    DOMAIN,
    IMPORT_PHASES,
    MAX_HISTORY_DAYS,
    NAME,
)
from .identity import (
    canonical_account,
    contracts_from_response,
    contracts_include,
    token_matches_account,
)
from .settings import effective_settings, validate_history_days
from .statistics import (
    HOURLY_INTERVAL,
    PROVIDER_TIME_ZONE,
    Frequency,
    add_hourly_requested_range,
    build_energy_projection,
    build_statistics,
    describe_hourly_coverage,
    describe_interval_coverage,
    initial_energy_hourly_since,
    migrate_legacy_hourly_ledger,
    normalize_readings,
    pending_hourly_provider_days,
    plan_windows,
    reconcile_intervals,
    validate_hourly_import_range,
    validate_hourly_requested_ranges,
    validate_saved_ledger,
)

_LOGGER = logging.getLogger(__name__)
MAX_FETCH_ATTEMPTS = 5
MAX_INLINE_RETRY_SECONDS = 60
MIN_REQUEST_INTERVAL_SECONDS = 1
HOURLY_INTERVAL_VERSION = 2
ENERGY_SERIES = "ENERGY"
ENERGY_PROJECTION_VERSION = 1


def _empty_import_status():
    """Return one independent, complete status snapshot for a new import."""
    return {
        "phase": "idle",
        "frequency": None,
        "current_window_start": None,
        "current_window_end": None,
        "completed_windows": 0,
        "total_windows": 0,
        "last_error": None,
    }


def _empty_hourly_reconciliation(pending_days=0):
    """Use one operational status shape for manual and background imports."""
    return {
        "pending_days": pending_days,
        "request_limit": MAX_HOURLY_BACKFILL_REQUESTS,
        "attempted_requests": 0,
        "completed_days": 0,
        "last_error": None,
    }


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

    entry.async_on_unload(async_at_start(hass, first_refresh))


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
        self.energy_statistic_id = f"{DOMAIN}:{self.id}_energy_consumption"
        self._energy_hourly_since = None
        self._energy_projection_initialized = False
        self._hourly_backfill_attempts = {}
        self._hourly_requested_ranges = []
        provider_today = dt_util.now().astimezone(PROVIDER_TIME_ZONE).date()
        self.hourly_import_start = provider_today - timedelta(days=7)
        self.hourly_import_end = provider_today
        self.entry_id = entry.entry_id
        self._should_import_history = settings[CONF_SHOULD_IMPORT_HISTORY]
        self._lock = asyncio.Lock()
        domain_data = hass.data.setdefault(DOMAIN, {})
        self._token_lock = domain_data.setdefault("_token_lock", asyncio.Lock())
        self._request_gate = domain_data.setdefault(
            "_request_gate", {"lock": asyncio.Lock(), "next_request_at": 0}
        )
        self._store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.{self.id}")
        self._ledger = None
        self._hourly_replay_pending = False
        self._stopping = False
        self._manual_import_reserved = False
        self._data = domain_data.setdefault(self.contract, {})
        self._data["coordinator"] = self
        self._data["import_status"] = _empty_import_status()
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

    @callback
    def _set_import_status(self, **changes) -> None:
        """Publish progress to listeners without scheduling another provider fetch."""
        if "phase" in changes and changes["phase"] not in IMPORT_PHASES:
            raise ValueError("Invalid History import phase")
        self._data["import_status"] = {**self._data["import_status"], **changes}
        if not self._stopping and not getattr(self, "_shutdown_requested", False):
            self.async_update_listeners()

    @callback
    def _set_manual_import_reserved(self, reserved: bool) -> None:
        """Reserve manual work before yielding so repeated clicks cannot queue it."""
        if reserved:
            self._assert_active()
            if self.import_in_progress:
                raise HomeAssistantError(
                    "A History import is already running. Wait for it to finish."
                )
        self._manual_import_reserved = reserved
        if not self._stopping and not getattr(self, "_shutdown_requested", False):
            self.async_update_listeners()

    @property
    def import_in_progress(self) -> bool:
        """Keep UI availability and backend duplicate protection consistent."""
        return self._manual_import_reserved or self._data["import_status"]["phase"] in {
            "running",
            "retrying",
        }

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

    def _existing_session_tokens(self, account: str):
        """Read only configured account/token fields, never another integration's runtime."""
        tokens = []
        for domain in ("aigues_barcelona", DOMAIN):
            for source in self.hass.config_entries.async_entries(domain):
                if source.entry_id == self.entry_id:
                    continue
                try:
                    matches = canonical_account(source.data.get(CONF_USERNAME, "")) == account
                except ValueError:
                    matches = False
                if matches:
                    tokens.append(source.data.get(CONF_TOKEN))
        tokens.extend((self._entry().data.get(CONF_TOKEN), self._api.get_token()))
        return list(
            dict.fromkeys(
                token.strip() for token in tokens if isinstance(token, str) and token.strip()
            )
        )

    async def _reuse_existing_session(self) -> bool:
        """Adopt only a fresh same-account session validated by the provider."""
        entry = self._entry()
        try:
            account = canonical_account(entry.data.get(CONF_USERNAME, ""))
        except ValueError:
            return False
        if not account:
            return False
        for token in self._existing_session_tokens(account):
            self._assert_active()
            # Validation never receives another entry's password or CAPTCHA key.
            candidate = AiguesBarcelonaHistoryApiClient(account, "", "", self.contract)
            try:
                candidate.set_token(token)
                if not token_matches_account(candidate, account) or candidate.is_token_expired():
                    continue
                try:
                    async with self._provider_request_slot():
                        if candidate.is_token_expired():
                            continue
                        rows = await self.hass.async_add_executor_job(candidate.contracts, account)
                    self._assert_active()
                except AiguesApiSessionLimitError:
                    self._update_entry_data(**{CONF_AUTH_SESSION_LIMIT: True})
                    continue
                except AiguesApiAuthError:
                    self._assert_active()
                    continue
                except (AiguesApiError, RequestException) as err:
                    self._assert_active()
                    self._persist_retry_pause(err)
                    raise
                try:
                    available = contracts_from_response(rows)
                    required = canonical_contracts(entry.data[CONF_CONTRACT])
                except KeyError, TypeError, ValueError:
                    raise AiguesApiError("Provider session contract validation failed") from None
                if not required or not contracts_include(available, required):
                    continue
                # A near-expiry session must still be usable after the GET returns.
                if not token_matches_account(candidate, account) or candidate.is_token_expired():
                    continue
                validated_token = candidate.get_token()
                if not isinstance(validated_token, str) or not validated_token:
                    continue
                self._assert_active()
                self._api.set_token(validated_token)
                self._update_entry_data(
                    **{
                        CONF_TOKEN: validated_token,
                        CONF_AUTH_SESSION_LIMIT: False,
                    }
                )
                _LOGGER.info("Reused a provider-validated configured Aigües session")
                return True
            finally:
                candidate.cli.close()
        return False

    async def _ensure_token(self) -> None:
        """Prefer existing sessions and never retry a known blocked paid login."""
        async with self._token_lock:
            self._assert_active()
            entry = self._entry()
            if not entry.data.get(CONF_AUTH_SESSION_LIMIT, False):
                if not self._api.is_token_expired():
                    return
                if stored := entry.data.get(CONF_TOKEN):
                    self._api.set_token(stored)
                    if not self._api.is_token_expired():
                        return
            if await self._reuse_existing_session():
                return
            if entry.data.get(CONF_AUTH_SESSION_LIMIT, False):
                raise self._session_limit_failure()
            if not entry.data.get(CONF_PASSWORD) or not entry.data.get(CONF_2CAPTCHA_APIKEY):
                raise ConfigEntryAuthFailed("Provide a current token or sign-in credentials")
            try:
                login_ok = await self.hass.async_add_executor_job(self._api.login)
            except AiguesApiSessionLimitError as err:
                self._update_entry_data(**{CONF_AUTH_SESSION_LIMIT: True})
                # Another configured entry may have renewed its session while our
                # one paid attempt was in flight. Recheck once, never buy a retry.
                if await self._reuse_existing_session():
                    return
                raise self._session_limit_failure() from err
            self._assert_active()
            token = self._api.get_token()
            if not login_ok or not token:
                raise RuntimeError("Aigües login did not return a token")
            if self._api.is_token_expired():
                raise RuntimeError("Aigües login returned an expired or invalid token")
            self._update_entry_data(**{CONF_TOKEN: token})

    async def _async_update_data(self):
        try:
            if self._should_import_history:
                await self.import_configured_consumptions(clear_startup_request=True)
            else:
                today = dt_util.now().date()
                await self._import_range(
                    today - timedelta(days=REGULAR_HOURLY_LOOKBACK_DAYS),
                    today + timedelta(days=1),
                    tuple(Frequency),
                )
                await self._recheck_incomplete_hours()
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
            if not isinstance(saved, dict):
                raise ValueError("Saved History intervals must be a mapping")
            self._hourly_requested_ranges = validate_hourly_requested_ranges(
                saved.get("hourly_requested_ranges", [])
            )
            try:
                self._hourly_backfill_attempts = validate_hourly_backfill_attempts(
                    saved.get("hourly_backfill_attempts", {})
                )
            except ValueError:
                # Scheduling metadata is recoverable independently of readings.
                self._hourly_backfill_attempts = {}
                _LOGGER.warning("Reset invalid saved hourly backfill scheduling metadata")
            energy_version = saved.get("energy_projection_version")
            if energy_version is None and self.energy_statistic_id in await self._stored_metadata():
                raise ValueError(
                    "Saved Energy projection boundary is missing; restore its source backup"
                )
            if energy_version is not None:
                if type(energy_version) is not int or energy_version != ENERGY_PROJECTION_VERSION:
                    raise ValueError("Unsupported saved Energy projection format")
                if "energy_hourly_since" not in saved:
                    raise ValueError("Saved Energy detail boundary is missing")
                since = saved.get("energy_hourly_since")
                if since is not None:
                    if not isinstance(since, str):
                        raise ValueError("Saved Energy detail boundary must be a calendar date")
                    self._energy_hourly_since = date.fromisoformat(since)
                    if self._energy_hourly_since.isoformat() != since:
                        raise ValueError("Saved Energy detail boundary must be a canonical date")
                self._energy_projection_initialized = True
            version = saved.get("hourly_interval_version", 1)
            pending = saved.get("hourly_replay_pending", False)
            if (
                not isinstance(version, int)
                or isinstance(version, bool)
                or version not in (1, HOURLY_INTERVAL_VERSION)
                or not isinstance(pending, bool)
                or (version == 1 and pending)
            ):
                raise ValueError("Unsupported saved hourly interval format")
            if pending and saved.get("hourly_replay_source_sha256") != self._hourly_source_sha256(
                saved[Frequency.HOURLY.value]
            ):
                raise ValueError(
                    "Pending hourly replay source checksum does not match saved intervals"
                )
            candidate = dict(saved)
            if version == 1 and saved[Frequency.HOURLY.value]:
                candidate[Frequency.HOURLY.value] = migrate_legacy_hourly_ledger(
                    saved[Frequency.HOURLY.value], dt_util.DEFAULT_TIME_ZONE
                )
                candidate, _ = self._prepare_statistics(candidate)
                await self._check_replay_coverage(saved)
                await self._save_ledger(candidate, replay_pending=True)
            else:
                candidate, _ = self._prepare_statistics(candidate)
                self._hourly_replay_pending = pending
                if any(candidate.values()) and (
                    not self._energy_projection_initialized
                    or (self._energy_hourly_since is None and candidate[Frequency.HOURLY.value])
                ):
                    await self._check_replay_coverage(candidate)
                    await self._save_ledger(candidate)
            self._ledger = candidate

    async def _save_ledger(
        self, ledger, *, replay_pending=None, backfill_attempts=None, requested_ranges=None
    ) -> None:
        """Persist source intervals and replay state together before recorder writes."""
        if replay_pending is None:
            replay_pending = getattr(self, "_hourly_replay_pending", False)
        since = self._energy_hourly_since
        if not self._energy_projection_initialized or (
            self._energy_hourly_since is None and ledger[Frequency.HOURLY.value]
        ):
            since = initial_energy_hourly_since(
                ledger[Frequency.HOURLY.value], dt_util.DEFAULT_TIME_ZONE
            )
        attempts = validate_hourly_backfill_attempts(
            self._hourly_backfill_attempts if backfill_attempts is None else backfill_attempts
        )
        requested = validate_hourly_requested_ranges(
            self._hourly_requested_ranges if requested_ranges is None else requested_ranges
        )
        pending = {
            day.isoformat()
            for day in pending_hourly_provider_days(
                ledger[Frequency.DAILY.value],
                ledger[Frequency.HOURLY.value],
                dt_util.DEFAULT_TIME_ZONE,
                hourly_since=since,
                current_provider_date=dt_util.now().astimezone(PROVIDER_TIME_ZONE).date(),
                requested_ranges=requested,
            )
        }
        attempts = {day: attempted_at for day, attempted_at in attempts.items() if day in pending}
        saved = {
            **ledger,
            "hourly_interval_version": HOURLY_INTERVAL_VERSION,
            "hourly_replay_pending": replay_pending,
            "energy_projection_version": ENERGY_PROJECTION_VERSION,
            "energy_hourly_since": (since.isoformat() if since else None),
            "hourly_backfill_attempts": attempts,
            "hourly_requested_ranges": requested,
        }
        if replay_pending:
            saved["hourly_replay_source_sha256"] = self._hourly_source_sha256(
                ledger[Frequency.HOURLY.value]
            )
        await self._store.async_save(saved)
        self._hourly_replay_pending = replay_pending
        self._energy_hourly_since = since
        self._energy_projection_initialized = True
        self._hourly_backfill_attempts = attempts
        self._hourly_requested_ranges = requested

    @staticmethod
    def _hourly_source_sha256(hourly_ledger) -> str:
        """Bind a destructive replay to the exact durable source approved for it."""
        serialized = json.dumps(
            hourly_ledger, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return hashlib.sha256(serialized.encode()).hexdigest()

    @staticmethod
    def _assert_recorded_coverage(actual, expected_starts) -> None:
        if not actual.keys() <= expected_starts:
            raise ValueError(
                "History interval ledger is missing recorded intervals; "
                "restore its backup before importing"
            )

    async def _check_replay_coverage(self, ledger, *, allow_legacy_hours=False) -> None:
        """Protect both source histories before replacing the owned hourly series."""
        for frequency in Frequency:
            points = build_statistics(ledger[frequency.value])
            actual = await self._statistics_snapshot(frequency, points)
            expected = {point["start"].timestamp() for point in points}
            if allow_legacy_hours and frequency is Frequency.HOURLY:
                expected |= {timestamp + HOURLY_INTERVAL.total_seconds() for timestamp in expected}
            self._assert_recorded_coverage(actual, expected)

    async def _wait_for_request(self, backoff: float, *, minimum_delay: float = 0) -> None:
        while True:
            self._assert_active()
            deadline = self._entry().data.get(CONF_RETRY_NOT_BEFORE, 0)
            if (
                isinstance(deadline, bool)
                or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline)
            ):
                raise ValueError("Invalid stored provider retry deadline")
            delay = max(backoff, deadline - time.time(), minimum_delay)
            if delay > MAX_INLINE_RETRY_SECONDS:
                raise AiguesApiRateLimitError(
                    "Provider retry pause is still active", retry_after=delay
                )
            if delay <= 0:
                return
            await asyncio.sleep(delay)
            backoff = 0
            minimum_delay = 0

    @asynccontextmanager
    async def _provider_request_slot(self):
        """Share one paced GET slot; callers must authenticate before acquiring it."""
        async with self._request_gate["lock"]:
            await self._wait_for_request(
                0, minimum_delay=self._request_gate["next_request_at"] - monotonic()
            )
            try:
                yield
            finally:
                self._request_gate["next_request_at"] = monotonic() + MIN_REQUEST_INTERVAL_SECONDS

    @asynccontextmanager
    async def _authenticated_request_slot(self):
        """Recheck expiry after waiting, renewing only outside the GET gate."""
        while True:
            needed_recovery = self._api.is_token_expired()
            await self._ensure_token()
            if self._api.is_token_expired():
                raise RuntimeError("Aigües authentication did not return a current token")
            async with self._provider_request_slot():
                if self._api.is_token_expired():
                    if needed_recovery:
                        raise RuntimeError(
                            "Aigües token expired while waiting for a provider request"
                        )
                    continue
                yield
                return

    def _persist_retry_pause(self, error) -> None:
        """Keep consumption and session validation pauses under one provider policy."""
        if (retry_after := getattr(error, "retry_after", None)) is not None:
            deadline = max(
                self._entry().data.get(CONF_RETRY_NOT_BEFORE, 0), time.time() + retry_after
            )
            self._update_entry_data(**{CONF_RETRY_NOT_BEFORE: deadline})

    async def _request_consumptions(
        self,
        start: date,
        end: date,
        frequency: Frequency,
        *,
        max_attempts: int = MAX_FETCH_ATTEMPTS,
        on_attempt=None,
    ) -> list:
        """Pace History GETs and retry transient failures without shortening pauses."""
        request = partial(
            self._api.consumptions,
            start,
            end,
            self.contract,
            frequency=frequency.value,
        )
        if type(max_attempts) is not int or not 1 <= max_attempts <= MAX_FETCH_ATTEMPTS:
            raise ValueError("Invalid consumption request attempt limit")
        for attempt in range(1, max_attempts + 1):
            await self._wait_for_request(0 if attempt == 1 else 2 ** (attempt - 2))
            async with self._authenticated_request_slot():
                self._assert_active()
                try:
                    if on_attempt is not None:
                        on_attempt()
                    rows = await self.hass.async_add_executor_job(request)
                    self._assert_active()
                    deadline = self._entry().data.get(CONF_RETRY_NOT_BEFORE, 0)
                    if deadline and deadline <= time.time():
                        self._update_entry_data(**{CONF_RETRY_NOT_BEFORE: 0})
                    self._set_import_status(phase="running", last_error=None)
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
                    self._set_import_status(phase="retrying", last_error=type(err).__name__)
                    self._persist_retry_pause(err)
                    if attempt == max_attempts:
                        raise
                    _LOGGER.warning(
                        "%s consumption request for %s (%s to %s) failed with %s; attempt %d/%d",
                        frequency.value,
                        self.contract,
                        start,
                        end,
                        type(err).__name__,
                        attempt,
                        max_attempts,
                    )

    async def _recheck_incomplete_hours(self) -> None:
        """Retry a bounded old backlog after fresh readings are already durable."""
        async with self._lock:
            self._assert_active()
            if self._manual_import_reserved:
                return
            await self._load_ledger()
            now = dt_util.now()
            provider_today = now.astimezone(PROVIDER_TIME_ZONE).date()
            pending = pending_hourly_provider_days(
                self._ledger[Frequency.DAILY.value],
                self._ledger[Frequency.HOURLY.value],
                dt_util.DEFAULT_TIME_ZONE,
                hourly_since=self._energy_hourly_since,
                current_provider_date=provider_today,
                requested_ranges=self._hourly_requested_ranges,
            )
            selected = plan_hourly_backfill(
                pending,
                self._hourly_backfill_attempts,
                now=now.timestamp(),
                current_provider_date=provider_today,
            )
            status = _empty_hourly_reconciliation(len(pending))
            self._data["hourly_reconciliation"] = status
            original_status = dict(self._data["import_status"])
            try:
                for day in selected:
                    self._assert_active()
                    if self._manual_import_reserved:
                        break
                    self._set_import_status(
                        phase="running",
                        frequency=Frequency.HOURLY.value,
                        current_window_start=day.isoformat(),
                        current_window_end=(day + timedelta(days=1)).isoformat(),
                    )
                    attempted = False

                    def mark_attempt():
                        nonlocal attempted
                        attempted = True

                    candidate = dict(self._ledger)
                    error = None
                    try:
                        rows = await self._request_consumptions(
                            day,
                            day + timedelta(days=1),
                            Frequency.HOURLY,
                            max_attempts=1,
                            on_attempt=mark_attempt,
                        )
                        incoming = normalize_readings(
                            rows,
                            Frequency.HOURLY,
                            dt_util.DEFAULT_TIME_ZONE,
                            provider_date=day,
                        )
                        # The exact provider-date filter owns the correction scope.
                        candidate[Frequency.HOURLY.value] = (
                            candidate[Frequency.HOURLY.value] | incoming
                        )
                        candidate, _ = self._prepare_statistics(candidate)
                    except AiguesApiAuthError, ConfigEntryAuthFailed:
                        raise
                    except (AiguesApiError, RequestException, ValueError) as err:
                        candidate = dict(self._ledger)
                        error = err
                        status["last_error"] = type(err).__name__
                        _LOGGER.warning(
                            "Deferred hourly backfill for %s (%s): %s",
                            self.contract,
                            day,
                            type(err).__name__,
                        )
                    self._assert_active()
                    if attempted:
                        status["attempted_requests"] += 1
                        attempts = {
                            **self._hourly_backfill_attempts,
                            day.isoformat(): dt_util.now().timestamp(),
                        }
                        await self._save_ledger(candidate, backfill_attempts=attempts)
                        self._ledger = candidate
                        await self._publish()
                        self._update_display()
                        still_pending = pending_hourly_provider_days(
                            candidate[Frequency.DAILY.value],
                            candidate[Frequency.HOURLY.value],
                            dt_util.DEFAULT_TIME_ZONE,
                            hourly_since=self._energy_hourly_since,
                            current_provider_date=provider_today,
                            requested_ranges=self._hourly_requested_ranges,
                        )
                        status["completed_days"] += int(day not in still_pending)
                        status["pending_days"] = len(still_pending)
                    if isinstance(error, AiguesApiRateLimitError) or not attempted:
                        break
            finally:
                self._data["import_status"] = original_status
                if not self._stopping and not getattr(self, "_shutdown_requested", False):
                    self.async_update_listeners()

    def _fetch_windows(self, start: date, end: date, frequency: Frequency, *, provider_dates=False):
        """Use one request plan for fetching and visible progress totals."""
        if start >= end:
            plan_windows(start, end, frequency)  # Validate reversed or invalid bounds.
            return []
        query_begin, query_stop = start, end
        if frequency is Frequency.HOURLY and not provider_dates:
            # Each reading closes the preceding UTC hour. Include the closing
            # reading at the requested end boundary, also across DST changes.
            first_end = dt_util.as_utc(dt_util.start_of_local_day(start)) + HOURLY_INTERVAL
            last_end = dt_util.as_utc(dt_util.start_of_local_day(end))
            query_begin = first_end.astimezone(PROVIDER_TIME_ZONE).date()
            query_stop = last_end.astimezone(PROVIDER_TIME_ZONE).date() + timedelta(days=1)
        provider_today = dt_util.now().astimezone(PROVIDER_TIME_ZONE).date()
        query_stop = min(query_stop, provider_today + timedelta(days=1))
        if query_begin >= query_stop:
            return []
        return plan_windows(
            query_begin,
            query_stop,
            frequency,
            split_at=provider_today,
        )

    async def _fetch(
        self, start: date, end: date, frequency: Frequency, *, windows=None, provider_dates=False
    ) -> dict:
        result = {}
        windows = (
            self._fetch_windows(start, end, frequency, provider_dates=provider_dates)
            if windows is None
            else windows
        )
        for query_start, query_end in windows:
            self._set_import_status(
                phase="running",
                frequency=frequency.value,
                current_window_start=query_start.isoformat(),
                current_window_end=query_end.isoformat(),
                last_error=None,
            )
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
                provider_dates=provider_dates,
            )
            result = reconcile_intervals(
                result,
                incoming,
                frequency,
                dt_util.DEFAULT_TIME_ZONE,
                startdate=start,
                enddate=end,
                provider_dates=provider_dates,
            )
            self._set_import_status(
                completed_windows=self._data["import_status"]["completed_windows"] + 1
            )
        return result

    async def _import_range(self, start: date, end: date, frequencies: tuple) -> None:
        await self._import_ranges({frequency: (start, end) for frequency in frequencies})

    async def _import_ranges(self, ranges: dict, *, provider_dates=False) -> None:
        """Commit different hourly/reference depths as one validated batch."""
        async with self._lock:
            if provider_dates and set(ranges) != {Frequency.HOURLY}:
                raise ValueError("Provider-date range imports require hourly history only")
            plans = {
                frequency: self._fetch_windows(start, end, frequency, provider_dates=provider_dates)
                for frequency, (start, end) in ranges.items()
            }
            self._data["import_status"] = _empty_import_status()
            self._set_import_status(
                phase="running", total_windows=sum(len(windows) for windows in plans.values())
            )
            try:
                self._assert_active()
                await self._load_ledger()
                if provider_dates:
                    requested = add_hourly_requested_range(
                        self._hourly_requested_ranges, *ranges[Frequency.HOURLY]
                    )
                    # Remember explicit intent before provider I/O, including
                    # dates for which the API returns no observations at all.
                    await self._save_ledger(self._ledger, requested_ranges=requested)
                await self._publish()
                self._update_display()
                # Verified cached readings remain useful while a long import runs.
                self.async_set_updated_data(self._data)
                await self._wait_for_request(0)
                candidate = dict(self._ledger)
                for frequency, (start, end) in ranges.items():
                    incoming = await self._fetch(
                        start,
                        end,
                        frequency,
                        windows=plans[frequency],
                        provider_dates=provider_dates,
                    )
                    candidate[frequency.value] = reconcile_intervals(
                        candidate[frequency.value],
                        incoming,
                        frequency,
                        dt_util.DEFAULT_TIME_ZONE,
                        startdate=start,
                        enddate=end,
                        provider_dates=provider_dates,
                    )
                candidate, _ = self._prepare_statistics(candidate)
                self._assert_active()
                await self._save_ledger(candidate)
                self._ledger = candidate
                await self._publish()
                self._update_display()
            except (Exception, asyncio.CancelledError) as err:
                self._set_import_status(phase="failed", last_error=type(err).__name__)
                raise
            else:
                self._set_import_status(phase="completed", last_error=None)

    async def _statistics_snapshot(self, frequency, points):
        statistic_id = self._statistic_id(frequency)
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
        roles = {
            Frequency.DAILY: "Provider daily totals (reference)",
            Frequency.HOURLY: "Water consumption (reported hours)",
            ENERGY_SERIES: "Water consumption",
        }
        return {
            "mean_type": 0,
            "unit_class": VolumeConverter.UNIT_CLASS,
            "has_sum": True,
            "name": f"{NAME} {self.id}: {roles[frequency]}",
            "source": DOMAIN,
            "statistic_id": self._statistic_id(frequency),
            "unit_of_measurement": UnitOfVolume.CUBIC_METERS,
        }

    async def _stored_metadata(self):
        return await get_instance(self.hass).async_add_executor_job(
            partial(
                get_metadata,
                self.hass,
                statistic_ids={*self.statistic_ids.values(), self.energy_statistic_id},
            )
        )

    def _statistic_id(self, series):
        return self.energy_statistic_id if series == ENERGY_SERIES else self.statistic_ids[series]

    def _energy_projection(self):
        return build_energy_projection(
            self._ledger[Frequency.DAILY.value],
            self._ledger[Frequency.HOURLY.value],
            dt_util.DEFAULT_TIME_ZONE,
            hourly_since=self._energy_hourly_since,
            use_daily_totals_for_incomplete=effective_settings(self._entry())[
                CONF_ENERGY_DAILY_FALLBACK
            ],
            current_provider_date=dt_util.now().astimezone(PROVIDER_TIME_ZONE).date(),
        )

    async def _publish(self) -> None:
        """Trust recorder readback, not successful queueing or an in-memory cache."""
        self._assert_active()
        _, statistics = self._prepare_statistics(self._ledger)
        energy, coverage = self._energy_projection()
        statistics[ENERGY_SERIES] = build_statistics(energy)
        replay_pending = getattr(self, "_hourly_replay_pending", False)
        if replay_pending:
            if not self._ledger[Frequency.HOURLY.value]:
                raise ValueError("Cannot replay an empty saved hourly history")
            await self._check_replay_coverage(self._ledger, allow_legacy_hours=True)
            self._assert_active()
            recorder = get_instance(self.hass)
            recorder.async_clear_statistics([self.statistic_ids[Frequency.HOURLY]])
            await recorder.async_block_till_done()
            self._assert_active()
        stored_metadata = await self._stored_metadata()
        metadata = {series: self._statistic_metadata(series) for series in statistics}
        expected = {
            frequency: {
                point["start"].timestamp(): (point["state"], point["sum"]) for point in points
            }
            for frequency, points in statistics.items()
        }
        changed = []
        # Validate both authoritative source series before any derived replacement.
        snapshots = {}
        for frequency in Frequency:
            snapshots[frequency] = await self._statistics_snapshot(frequency, statistics[frequency])
            self._assert_recorded_coverage(snapshots[frequency], expected[frequency].keys())
        snapshots[ENERGY_SERIES] = await self._statistics_snapshot(
            ENERGY_SERIES, statistics[ENERGY_SERIES]
        )
        if snapshots[ENERGY_SERIES] and not any(self._ledger.values()):
            raise ValueError("Energy source history is missing; restore its source backup")
        if not snapshots[ENERGY_SERIES].keys() <= expected[ENERGY_SERIES].keys():
            # This view can be rebuilt from verified durable raw histories after
            # a precision/policy change removes old aggregate or partial keys.
            self._assert_active()
            recorder = get_instance(self.hass)
            recorder.async_clear_statistics([self.energy_statistic_id])
            await recorder.async_block_till_done()
            self._assert_active()
            snapshots[ENERGY_SERIES] = await self._statistics_snapshot(
                ENERGY_SERIES, statistics[ENERGY_SERIES]
            )
            if snapshots[ENERGY_SERIES]:
                raise UpdateFailed("Recorder did not clear the derived Energy series")
            stored_metadata = await self._stored_metadata()
        for frequency, points in statistics.items():
            actual = snapshots[frequency]
            stored = stored_metadata.get(self._statistic_id(frequency))
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
                stored = stored_metadata.get(self._statistic_id(frequency))
                if stored is None or stored[1]["name"] != metadata[frequency]["name"]:
                    raise UpdateFailed("Recorder did not persist the History series name")
        if replay_pending:
            await self._save_ledger(self._ledger, replay_pending=False)
        self._data["energy_coverage"] = coverage

    def _update_display(self) -> None:
        pending = pending_hourly_provider_days(
            self._ledger[Frequency.DAILY.value],
            self._ledger[Frequency.HOURLY.value],
            dt_util.DEFAULT_TIME_ZONE,
            hourly_since=self._energy_hourly_since,
            current_provider_date=dt_util.now().astimezone(PROVIDER_TIME_ZONE).date(),
            requested_ranges=self._hourly_requested_ranges,
        )
        self._data.setdefault("hourly_reconciliation", _empty_hourly_reconciliation())[
            "pending_days"
        ] = len(pending)
        readings = [row for ledger in self._ledger.values() for row in ledger.values()]
        if readings:
            latest = max(readings, key=lambda row: datetime.fromisoformat(row["observed_at"]))
            self._data[CONF_VALUE] = latest["state"]
            self._data[CONF_STATE] = latest["observed_at"]
        self._data["statistics"] = {
            "energy": self.energy_statistic_id,
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
        self._data["provider_daily_coverage"] = describe_interval_coverage(
            self._ledger[Frequency.DAILY.value]
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

    async def import_hourly_date_range(self, start_date, end_date) -> None:
        """Import an inclusive range of provider reporting dates and retain its intent."""
        try:
            start, stop = validate_hourly_import_range(
                start_date,
                end_date,
                today_provider=dt_util.now().astimezone(PROVIDER_TIME_ZONE).date(),
                max_days=MAX_HISTORY_DAYS,
            )
        except ValueError as error:
            raise HomeAssistantError(str(error)) from error
        await self._import_ranges({Frequency.HOURLY: (start, stop)}, provider_dates=True)
        self.async_set_updated_data(self._data)

    async def async_import_hourly_date_range(self) -> None:
        """Run the selected device dates through the shared manual-import lifecycle."""
        from .service import async_run_import

        await async_run_import(
            self.hass,
            self,
            frequency=Frequency.HOURLY.value,
            start_date=self.hourly_import_start,
            end_date=self.hourly_import_end,
        )

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
            "provider_daily_coverage": self.coordinator._data.get("provider_daily_coverage", {}),
            "energy_coverage": self.coordinator._data.get("energy_coverage", {}),
            "import_status": self.coordinator._data.get("import_status", _empty_import_status()),
            "hourly_reconciliation": self.coordinator._data.get("hourly_reconciliation", {}),
        }
