# Aigües Barcelona History

[![CI](https://github.com/rndm2/aigues_barcelona_history/actions/workflows/ci.yml/badge.svg)](https://github.com/rndm2/aigues_barcelona_history/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/rndm2/aigues_barcelona_history)](https://github.com/rndm2/aigues_barcelona_history/releases/latest)
[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](LICENSE)

Import Aigües de Barcelona water history into Home Assistant, with hourly
consumption, local calendar-day totals and repeatable corrections.

**Requires Home Assistant 2026.10.4 or newer.** Automated regression tests run
against 2026.10.4. This unofficial integration uses the private customer-portal API.

## Features

- One Energy statistic combining older daily aggregates with available actual hours.
- Separate reported-hour and provider daily-reference statistics for inspection.
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

During background updates, History checks for an unexpired session already
configured by an Aigües integration for the same account before buying another
login. The provider must confirm access to every configured History contract.
History updates only its own entry, without changing or logging out the source
session. Other integrations remain optional. If no usable session exists, a
confirmed session-limit block still prevents another automatic paid login.

## Settings and buttons

Setup asks for these settings after authentication. They are also available
through the integration's **Configure** action.

| Setting | Meaning |
| --- | --- |
| Hourly history to request | Past HA calendar days for hourly consumption, plus today. |
| Daily reference history to request | Past provider dates for daily reports, plus today when available. |
| Refresh interval | Recent-data refresh interval, in minutes. |
| Use daily totals when hours are incomplete | Show one daily aggregate in Energy until complete hours arrive. Disable it to retain partial recent hours instead. Older daily history remains available. |
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

Regular refreshes revisit **seven past days plus today**. They also retry known
incomplete older hourly reporting blocks without an age cutoff. Each refresh
allows at most two additional consumption GET attempts per meter, with the
oldest actual attempt served first. Days aged 8–30 are eligible daily, 31–180
weekly, and older days every 30 days. The attempt times survive restarts.

The backlog includes every known partial hourly block and missing blocks within
the detailed-history range. Older daily-only history remains compressed. A
valid empty response keeps its day pending. Returned hours are merged and can
correct saved values only within the requested provider date. An old request
failure does not discard the already committed recent readings. Backlog calls
share the normal request pacing and provider retry pauses; each scheduled old
attempt performs one consumption GET, while normal request retries are unchanged.
The sensor's `hourly_reconciliation` attribute reports pending days, attempts,
completed blocks and the latest background error class.

Use a button for older complete-day corrections or to revisit a larger range.
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
| Combined Energy history | `aigues_barcelona_history:example123_energy_consumption` |
| Canonical consumption | `aigues_barcelona_history:example123_hourly_consumption` |
| Provider daily reference | `aigues_barcelona_history:example123_consumption` |

Select **Water consumption** as the single Energy water source for
the meter. It retains older daily history and uses actual hours for complete
provider reporting blocks. Do not also select the source-only statistics or
another integration for the same meter, because Energy would add them again.

Older daily reports occupy one aggregate point per provider day. Native Energy
compresses them into monthly and yearly views. HA statistics have no per-point
duration field: opening an old date in hourly mode shows its entire daily total
as one aggregate bar, not measured consumption during that hour. History never
distributes it across 24 invented hours.

The current provider day always uses its reported hours and fills as more arrive.
By default, an incomplete past provider block can temporarily use its daily total
while waiting for complete actual hours. Disable **Use daily totals when hours
are incomplete** to keep partial recent hours and their incomplete totals. The detail
boundary is saved once, so an isolated older backfill cannot erase daily history
between it and the recent data. Complete older blocks can independently upgrade
to actual hours. Available daily reports cover the older history even without hours.
Regular polling revisits recent incomplete days and accepts newly returned hours
and corrections after a previous request completed. Completion of an HTTP request
does not mark that day's source history complete. Coverage distinguishes a current
day that is updating from past dates still awaiting hours.

A complete hourly block replaces its aggregate without adding both amounts.
Later cumulative sums are rebuilt. If a daily report is below consumption already
reported by its known hours, the actual hours take priority and coverage exposes
the disagreement. Provider fixed-CET blocks, rather than naive local dates,
define ownership so winter boundary hours are not counted twice or lost.

The display sensor shows the latest provider-reported meter reading. External
consumption statistics are published separately. Its `hourly_coverage` attribute
reports incomplete dates within the known span; it does not certify dates after
the latest returned hour. Its compact `energy_coverage` attribute describes
coarse days, actual hours and the active incomplete-day policy.

### View older reference history

The combined Energy statistic extends into older available daily history without
creating an hourly source history for those dates. The two source statistics stay
available for comparison. Requested dates with neither source remain absent.

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

Hourly timestamps retain their supplied offset. The provider frontend converts
these readings to Europe/Madrid and labels each point with the meter's reading
time. Its `deltaConsumption` describes consumption since the preceding reading.
HA statistics instead identify the **start** of the consumption period, with the
meter state at its end. History therefore assigns a reading to the preceding
UTC hour, then lets HA group those intervals by its local calendar. This is an
interval-end conversion, not a daylight-saving offset correction.

Europe/Madrid summer/winter boundaries and 23/25-hour days are tested. Requests
include the boundary readings needed to finish the requested HA days, including
the following provider date in winter. The current provider day remains separate
from historical queries. Missing hours are never filled from meter differences.

The provider's selected query date is not always an HA calendar day. Its observed
fixed-CET date convention can end a winter daily report around 23:00 Madrid. Thus
the separate **Provider daily totals (reference)** can differ from a civil HA day
even with complete hours. The combined series selects each provider block once.
Its daily/monthly views reduce the same selected points as its hourly view, while
older coarse points retain the provider's reporting-date meaning.
An Aigües point labeled 01:02 is a closing meter reading. Its consumption appears
in HA's preceding 00:00 interval, so the printed hour labels differ by design.

Version 0.4.4 repairs previously saved reading-time buckets once. It retains all
original readings and amounts, saves a replay marker, replaces only History's
hourly statistic through recorder, and verifies the replacement before marking
the repair complete. An interrupted replay is retried from the durable source.

An explicitly imported recorder archive can extend daily history with verified
consecutive meter increments. Its first cumulative reading is only a baseline,
never consumption. Archive records retain their source statistic, timestamp and
row digest. Original recorder statistics are read-only during such an import,
and actual later API observations can replace those dated archive records.

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
shared SQL transaction. Temporary consumption GET failures retry up to five attempts, with
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
