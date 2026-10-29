"""Install development dependencies and the authoritative manifest requirements."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "custom_components/aigues_barcelona_history/manifest.json"


def main() -> None:
    requirements = json.loads(MANIFEST.read_text())["requirements"]
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            str(ROOT / "requirements-dev.txt"),
            *requirements,
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
