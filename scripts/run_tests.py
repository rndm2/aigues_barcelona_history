"""Run portable offline History tests, allowing only in-memory SQLite."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import sqlite3
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def isolate_external_effects() -> None:
    """Keep real recorder tests isolated from networks and persistent databases."""
    original_connect = sqlite3.connect

    def blocked(*args, **kwargs):
        raise AssertionError("Offline tests forbid network access")

    def memory_database(database, *args, **kwargs):
        if database != ":memory:":
            raise AssertionError("Offline tests forbid file-backed databases")
        return original_connect(database, *args, **kwargs)

    socket.create_connection = blocked
    socket.getaddrinfo = blocked
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    sqlite3.connect = memory_database
    sqlite3.dbapi2.connect = memory_database
    for name in (
        "SUPERVISOR_TOKEN",
        "SUPERVISOR_CORE_API_SOCKET",
        "HASSIO_TOKEN",
        "HASS_TOKEN",
        "HA_TOKEN",
    ):
        os.environ.pop(name, None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", default="test*.py")
    args = parser.parse_args()
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    isolate_external_effects()

    # HA installs its voluptuous compatibility alias before integration imports.
    from homeassistant.const import __version__

    print(f"Offline runtime: HA {__version__}, Python {sys.version.split()[0]}", flush=True)
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern=args.pattern)
    return int(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())


if __name__ == "__main__":
    raise SystemExit(main())
