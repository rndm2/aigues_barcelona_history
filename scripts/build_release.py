"""Build a reproducible integration-only ZIP and its SHA-256 checksum."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/aigues_barcelona_history"


def build(output: Path, *, expected_version: str | None = None) -> tuple[Path, str]:
    """Include only integration files, never tests, local config or captures."""
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    version = manifest["version"]
    if expected_version is not None and expected_version.removeprefix("v") != version:
        raise ValueError("Release tag does not match manifest version")
    paths = []
    for path in sorted(COMPONENT.rglob("*")):
        if path.is_symlink():
            raise ValueError("Release archives cannot contain symlinks")
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix in {".pyc", ".pyo"} or any(
            part.startswith(".") for part in path.relative_to(COMPONENT).parts
        ):
            continue
        paths.append(path)
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"aigues_barcelona_history-{version}.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in paths:
            info = zipfile.ZipInfo(str(path.relative_to(ROOT)), date_time=(2020, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (output / "SHA256SUMS").write_text(f"{digest}  {target.name}\n")
    return target, digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--version", help="Optional release tag to check against the manifest")
    options = parser.parse_args()
    path, digest = build(options.output, expected_version=options.version)
    print(json.dumps({"archive": str(path), "sha256": digest}))


if __name__ == "__main__":
    main()
