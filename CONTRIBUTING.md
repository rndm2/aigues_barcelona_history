# Contributing

Use Python 3.14.2 or newer within the 3.14 series. Tests target HA 2026.10.4.
Create a virtual environment and run:

```sh
python3.14 -m venv .venv
.venv/bin/python scripts/bootstrap.py
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python scripts/run_tests.py
```

The bootstrap script reads integration requirements from the manifest. CI runs
the same checks. To build the manual-install ZIP and checksum:

```sh
.venv/bin/python scripts/build_release.py --version v0.4.1
```

For a later release, pass its intended tag. The builder rejects a tag that
disagrees with the manifest and excludes hidden files, caches and symlinks.

Keep shared validation and calculations in one authoritative implementation.
Preserve these guarantees:

- No invented hours or redistribution of daily totals.
- Corrections to returned intervals only, inside the requested range.
- Partial responses preserve saved readings; reimports do not duplicate consumption.
- UTC buckets and HA calendar boundaries remain consistent across DST.
- Entry unload cancels imports and prevents later persistence.
- Authentication errors cannot create a repeated paid-login loop.

Use synthetic accounts, contract IDs and consumption data. Tests must not contact
Aigües, solve CAPTCHAs, read an HA config directory or open a file-backed recorder
database. Real recorder tests use isolated in-memory SQLite.

Keep user-facing content, comments and documentation in English. Preserve existing
technical IDs unless a reviewed migration explicitly changes them.

Before a pull request, run regression and formatting checks. Describe the resulting
behavior and any API, statistic identity, storage or authentication changes.
Never include credentials, private traces, storage/database exports or household data.
