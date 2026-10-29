# Regression tests

Run `python scripts/run_tests.py` after installing dependencies with
`python scripts/bootstrap.py` in a Python 3.14 virtual environment.

All account identifiers and readings are synthetic. Calendar dates exercise
Europe/Madrid daylight-saving boundaries. Tests never need credentials, a
CAPTCHA balance or a running Home Assistant server.

The runner blocks network connections, DNS and file-backed SQLite. Most tests
use fake I/O. Recorder integrity and calendar tests use actual Home Assistant
ORM/import/query code with SQLite entirely in memory.

The original integration is represented only by namespace and service-handler
sentinels. No second installable integration is bundled or required.
