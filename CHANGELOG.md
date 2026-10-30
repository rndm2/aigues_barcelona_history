# Changelog

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
