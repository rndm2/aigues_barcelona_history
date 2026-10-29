"""Verify release boundaries, tag consistency and reproducible ZIP contents."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_release.py"
SPEC = importlib.util.spec_from_file_location("build_release", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class ReleasePackageTests(unittest.TestCase):
    def test_archive_is_reproducible_and_contains_only_the_integration(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            first, first_digest = release.build(folder / "first")
            second, second_digest = release.build(folder / "second")
            self.assertEqual(first_digest, second_digest)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                names = archive.namelist()
                self.assertTrue(names)
                self.assertTrue(
                    all(
                        name.startswith("custom_components/aigues_barcelona_history/")
                        for name in names
                    )
                )
                self.assertFalse(any("__pycache__" in name or "/tests/" in name for name in names))
                manifest = json.loads(
                    archive.read("custom_components/aigues_barcelona_history/manifest.json")
                )
                self.assertEqual(manifest["domain"], "aigues_barcelona_history")
            self.assertEqual(
                (first.parent / "SHA256SUMS").read_text(), f"{first_digest}  {first.name}\n"
            )

    def test_tag_mismatch_is_rejected_before_creating_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "release"
            with self.assertRaisesRegex(ValueError, "tag"):
                release.build(output, expected_version="v0.0.0")
            self.assertFalse(output.exists())

    def test_hidden_local_files_are_excluded_from_archives(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            component = root / "custom_components/aigues_barcelona_history"
            component.mkdir(parents=True)
            (component / "manifest.json").write_text('{"version":"1.0.0"}')
            (component / "__init__.py").write_text('"""Fixture integration."""\n')
            (component / ".private").mkdir()
            (component / ".private/settings.json").write_text('{"credential":"never-package"}')
            (component / ".env").write_text("TOKEN=never-package\n")
            with patch.object(release, "ROOT", root), patch.object(release, "COMPONENT", component):
                archive_path, _ = release.build(root / "dist")
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {
                        "custom_components/aigues_barcelona_history/manifest.json",
                        "custom_components/aigues_barcelona_history/__init__.py",
                    },
                )

    def test_symlinks_are_rejected_before_creating_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            component = root / "custom_components/aigues_barcelona_history"
            component.mkdir(parents=True)
            (component / "manifest.json").write_text('{"version":"1.0.0"}')
            private = root / "private.txt"
            private.write_text("never-package")
            (component / "linked.py").symlink_to(private)
            output = root / "dist"
            with patch.object(release, "ROOT", root), patch.object(release, "COMPONENT", component):
                with self.assertRaisesRegex(ValueError, "symlink"):
                    release.build(output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
