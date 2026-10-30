# Aigües Barcelona History

[![CI](https://github.com/rndm2/aigues_barcelona_history/actions/workflows/ci.yml/badge.svg)](https://github.com/rndm2/aigues_barcelona_history/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/rndm2/aigues_barcelona_history)](https://github.com/rndm2/aigues_barcelona_history/releases/latest)
[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](LICENSE)

Import Aigües de Barcelona water history into Home Assistant, with hourly
consumption, local calendar-day totals and repeatable corrections.

**Requires Home Assistant 2026.10.4 or newer.** Automated regression tests run
against 2026.10.4. This unofficial integration uses the private customer-portal API.

## Features

- Hourly external statistics that HA groups into days, months and years in Energy.
- Separate provider daily reports retained as a reference.
- Scoped corrections and backfills without duplicate consumption.
- Previously saved hours retained when responses are partial or empty.
- Separate import ranges, refresh minutes and two import buttons per contract.
- Visible import progress and failures, with duplicate manual runs rejected.
- Durable source intervals, recorder readback and temporary-request retries.
- Privacy-preserving diagnostics with settings, counts and status only.

The requested range does not guarantee available history. The provider may return
incomplete records or estimates. Missing hours remain absent.

## Install with HACS

[![Add this repository to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=rndm2&repository=aigues_barcelona_history&category=integration)

1. Open **HACS → Custom repositories**.
2. Add `https://github.com/rndm2/aigues_barcelona_history`, category **Integration**.
3. Download **Aigües Barcelona History**, then restart HA.
4. Open **Settings → Devices & services → Add integration** and select History.

This is a custom repository, not an entry in the default HACS catalog.
For manual installation, copy `custom_components/aigues_barcelona_history` into
`/config/custom_components/`, then restart HA.

### Adopt an existing local History installation

Back up the History component, config entry, source Store and recorder database.
Download this repository through HACS over the existing History folder, then
restart. Keep the integration configured; do not delete and recreate its entry.

The domain, config-flow version, entity/statistic IDs and Store identity are
retained. This repository contains only the History runtime component.
The original `aigues_barcelona` integration, its statistics and cleanup jobs remain
independent. History does not migrate or manage them.

## Authentication

Supply your DNI/NIE and either:

- Your portal password and **2Captcha API key**. A new login solves a paid CAPTCHA.
- An existing unexpired access token. It must belong to that account and is
  checked through a contracts request without creating a provider session.

Credentials and the token are saved in HA's config-entry storage. Never share
that storage or paste credentials into issues. Expired tokens use the configured
credentials during normal updates. A confirmed session-limit error pauses
automatic sign-in until successful recovery. Consumption GET retries do not
repeat a login or purchase additional CAPTCHAs.

## Settings and buttons

Setup asks for these settings after authentication. They are also available
through the integration's **Configure** action.

| Setting | Meaning |
| --- | --- |
| Hourly history to request | Past HA calendar days for hourly consumption, plus today. |
| Daily reference history to request | Past provider dates for daily reports, plus today when available. |
| Refresh interval | Recent-data refresh interval, in minutes. |
| Import both histories after setup/saving (once) | Run both configured ranges once. |

Each range accepts **1–1,825 past days**. New setups offer **365 days** each and
**65 minutes** between refreshes. The initial import is optional. Existing entries
retain their effective settings and pending-import state.

The contract's History device provides:

- **Import hourly history**: use its saved hourly range.
- **Import daily reference**: use its saved daily range.

Buttons preserve existing history and do not mark the other configured import
complete. Their timestamp records the press, not proof of complete provider
coverage. The configured initial request switches off after both imports succeed
for every contract. An empty valid response can complete a request without
providing the requested history.

Regular refreshes revisit **seven past days plus today**. They do not repeat the
entire configured range. Use a button for older gaps or provider corrections.
Changing settings reloads the entry; normal expired-token recovery still applies.

The meter's `import_status` attribute reports the current phase, request window
and completed/total windows. Loaded readings remain visible during a backfill.
A second manual import is rejected while work is pending or running. Wait for
the active job to finish before requesting another range.

## Energy

For example contract `EXAMPLE123`:

| Resource | Identifier |
| --- | --- |
| Display sensor | `sensor.aigues_barcelona_history_example123` |
| Canonical consumption | `aigues_barcelona_history:example123_hourly_consumption` |
| Provider daily reference | `aigues_barcelona_history:example123_consumption` |

Select **Water consumption (reported hours)** as the Energy water source. HA's
daily and hourly views then use the same intervals. The separate daily reference
can cover different day boundaries. Adding it, or another integration for the
same meter, as a second Energy source counts that meter again in the total.

The display sensor shows the latest provider-reported meter reading. External
consumption statistics are published separately. Its `hourly_coverage` attribute
reports incomplete dates within the known span; it does not certify dates after
the latest returned hour.

### View older reference history

Energy only contains the reported hours selected as its source. Importing the
separate daily reference does not create hourly history or extend Energy into
years where hours are unavailable.

Use the native **Statistics graph** card with the provider daily-reference ID,
`period: month` and `stat_types: [change]` to view monthly sums of available daily
reports. Use a separate card with the canonical hourly ID for recent hours.
The meter's `provider_daily_coverage` and `hourly_coverage` attributes show the
known source ranges. A successful request cannot guarantee that the provider
retains every requested date.

```yaml
type: statistics-graph
title: Monthly provider reference
days_to_show: 1825
period: month
chart_type: bar
stat_types:
  - change
entities:
  - entity: aigues_barcelona_history:example123_consumption
```

Portal invoice charts describe whole billing periods, which can span more than
one calendar month. Their end-date labels do not make the amounts monthly
consumption. Keep billed periods separate rather than splitting them into
invented monthly or hourly readings.

## Time, corrections and persistence

Hourly timestamps retain their supplied offset and map to UTC buckets. HA groups
them using its local calendar. Europe/Madrid summer/winter boundaries and
23/25-hour days are tested. Requests follow the observed fixed-CET provider-date
convention, include the preceding boundary date, and keep the current provider
day separate from historical queries.

New hours are inserted and returned hourly corrections replace saved values,
even if their observation minute moved backwards. Only returned source intervals
inside the requested range change. Missing response intervals remain saved.
The integration validates formats and arithmetic; it cannot independently verify
the physical accuracy of a plausible provider reading.

Each series accumulates `deltaConsumption` from its first known interval. The
lifetime meter reading remains separate as `state`. Corrections rebuild later
cumulative `sum` values while preserving unrelated later interval consumption.
Reimports are idempotent. Daily totals never fill unknown hours.

Source intervals live in `.storage/aigues_barcelona_history.<entry_id>.<contract>`.
Back up this Store together with recorder data. A missing/partial Store that
omits recorded intervals blocks a partial-history rebase and requires recovery.

Candidate source batches are validated before saving. After publication, recorder
rows are read back and compared by timestamp, state and sum. Failed publication
is replayed from durable source data on a later run. The two series are not one
shared SQL transaction. Temporary GET failures retry up to five attempts, with
1/2/4/8-second backoff and persisted provider Retry-After deadlines.
Consumption requests are also spaced by at least one second across History
meters. Longer provider pauses take precedence. Long imports check the token
between request windows rather than relying only on the token at startup.

## Explicit import action

Buttons use separate saved ranges. The legacy action supports a shared explicit
range for the selected frequencies:

```yaml
action: aigues_barcelona_history.import_historical_data
data:
  contract: EXAMPLE123
  history_days: 30
  frequency: HOURLY
```

Omit `contract` when only one is configured. `frequency` is `HOURLY`, `DAILY` or
`ALL` (default). `history_days` defaults to 365. The old `reset_and_refresh_data`
name is an import alias, not a statistics reset. Explicit-range actions retain
their legacy one-time-request completion behavior; use the buttons for
independent configured imports.

## Help and development

For missing hours, inspect coverage and request the relevant range. A successful
empty response is not a zero-consumption reading. For authentication limits,
recover a valid session before trying another login. For recorder integrity
errors, preserve the Store/database and restore a consistent backup.

Download the integration diagnostics when reporting a problem. They omit
credentials, identifiers and consumption traces. Review any extra logs or
screenshots before sharing them.

See [Contributing](CONTRIBUTING.md), [security reporting](SECURITY.md),
[changes](CHANGELOG.md) and [upstream provenance](ATTRIBUTION.md).

## License

[GPL-3.0](LICENSE), derived from
[`rndm2/aigues_barcelona`](https://github.com/rndm2/aigues_barcelona).
Original Git history and authorship are retained. This project is not affiliated
with Aigües de Barcelona.
