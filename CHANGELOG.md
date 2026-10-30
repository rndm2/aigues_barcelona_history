# Changelog

## 0.5.2 — 2026-10-30

- Add saved From/To date selectors and a manual hourly-range import button to each History device.
- Add a date-picker action for inclusive Aigües reporting dates, using the shared manual-import lifecycle and retry policy.
- Persist explicit request ranges before provider I/O, retaining old empty or failed dates for bounded background retries.
- Keep corrections scoped to requested provider dates and preserve existing HA-calendar day-count imports.
- Restore date selections and allow edits without changing a running job's captured range.

## 0.5.1 — 2026-10-30

- Retry incomplete older hourly reporting blocks without an age cutoff.
- Persist fair age-based scheduling, capped at two additional consumption attempts per refresh.
- Isolate old-request failures after recent readings are committed and preserve shared provider pacing and retry pauses.
- Reconcile exact provider dates, including their winter boundary hour on the preceding HA date.

## 0.5.0 — 2026-10-30

- Add one Energy statistic combining older daily aggregates with complete reported hours.
- Replace coarse blocks when complete hours arrive, without duplicate consumption.
- Keep both original History source series available and preserve their raw observations.
- Add a friendly option for full daily aggregates or partial recent hourly detail.
- Persist the detail boundary and protect replay from missing source metadata.
- Retain explicit provenance for validated archived daily increments, excluding unknown first baselines.

## 0.4.4 — 2026-10-30

- Assign hourly consumption to the interval preceding its meter reading, retaining the API offset and both daylight-saving folds.
- Fetch the following boundary reading when it is needed to complete a requested HA day.
- Repair saved hourly buckets and their recorder statistics once, with a durable replay marker and verified readback.
- Preserve all source amounts, missing hours, daily references and the original integration.
- Document why a provider daily report can use different boundaries from a civil HA day.

## 0.4.3 — 2026-10-30

- Reuse a server-validated, unexpired session for the same account before a background update buys another login.
- Verify all configured contracts and update only History's own authentication data.
- Recover a session-limit race when another integration has just renewed a usable session.
- Keep the paid-login block durable during cancellation and preserve request pacing and token-expiry checks.

## 0.4.2 — 2026-10-30

- Fix startup-listener cleanup during reload and unloading.
- Show import progress, completion and failures while preserving cached readings.
- Reject duplicate manual imports instead of silently queuing another backfill.
- Pace consumption requests and check token expiry between historical windows.
- Expose daily-reference coverage alongside hourly coverage.
- Document separate monthly reference and recent hourly history views.

## 0.4.1 — 2026-10-30

First standalone public History release.

- Add HACS metadata, tested HA 2026.10.4 minimum and an exact 2Captcha dependency.
- Add privacy-preserving diagnostics, documentation and repository templates.
- Add portable regression tests and automated validation.
- Retain current config entries, entity/statistic IDs and source Store identity.
- Preserve locally deployed 0.4.0 import, correction, time-boundary and retry behavior.

## Earlier local versions

These were local versions, not standalone public releases in this repository.

- **0.4.0:** separate import depths, refresh minutes, initial-import settings and buttons.
- **0.3.1:** canonical hourly series for HA day/hour views, corrections and coverage.
- **0.3.0:** durable validated source ledger, recorder readback, retries and authentication guards.

Upstream changes remain available in the retained Git history.
