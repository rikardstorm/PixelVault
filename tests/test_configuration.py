import importlib.util
import json
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from unittest.mock import patch

APP = pathlib.Path(__file__).resolve().parents[1] / "pixel_uploader.py"
spec = importlib.util.spec_from_file_location("config_uploader", APP)
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


class ConfigurationTests(unittest.TestCase):
    def fixture(self, folder):
        root = pathlib.Path(folder)
        archive = root / "takeout.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("Takeout/Photos/a.jpg", b"synthetic-identical-content")
            z.writestr("Takeout/Photos/b.jpg", b"synthetic-identical-content")
            z.writestr(
                "Takeout/Photos/a.jpg.json",
                json.dumps(
                    {"title": "a.jpg", "photoTakenTime": {"timestamp": "1500000000"}}
                ),
            )
        config = {
            "account": "tester@example.invalid",
            "serial": "TEST_SERIAL",
            "archives": ["takeout.zip"],
            "tools": {
                "adb": "missing-adb-for-offline-test",
                "exiftool": "missing-exiftool-for-offline-test",
            },
        }
        path = root / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return root, path, config

    def test_config_paths_are_relative_to_config_location(self):
        with tempfile.TemporaryDirectory() as folder:
            root, path, _ = self.fixture(folder)
            with patch.object(u, "STATE", root / "state"):
                binding = u.configure(path)
                self.assertEqual(
                    binding["archives"][0]["path"],
                    str((root / "takeout.zip").resolve()),
                )

    def test_changed_account_cannot_reuse_saved_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root, path, config = self.fixture(folder)
            with patch.object(u, "STATE", root / "state"):
                binding = u.configure(path)
                u.atomic_json(u.STATE / "session-binding.json", binding)
                config["account"] = "different@example.invalid"
                path.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaises(ValueError):
                    u.configure(path)

    def test_changed_archive_cannot_reuse_saved_catalog(self):
        with tempfile.TemporaryDirectory() as folder:
            root, path, _ = self.fixture(folder)
            with patch.object(u, "STATE", root / "state"):
                u.atomic_json(u.STATE / "session-binding.json", u.configure(path))
                with (root / "takeout.zip").open("ab") as stream:
                    stream.write(b"changed")
                with self.assertRaises(ValueError):
                    u.configure(path)

    def test_shell_syntax_in_phone_folder_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, path, config = self.fixture(folder)
            config["phone_folder"] = "/storage/emulated/0/Pictures/test;unsafe"
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch.object(u, "STATE", root / "state"):
                with self.assertRaises(ValueError):
                    u.configure(path)

    def test_newer_pixel_is_rejected(self):
        with patch.object(u, "adb", return_value="newer-device"):
            with self.assertRaises(RuntimeError):
                u.verify_device()

    @unittest.skipUnless(sys.platform == "win32", "Windows CLI integration")
    def test_catalog_only_works_without_adb_and_keeps_archive_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            root, path, _ = self.fixture(folder)
            shutil.copy2(APP, root / APP.name)
            before = u.file_hash(root / "takeout.zip")
            result = subprocess.run(
                [
                    sys.executable,
                    str(root / APP.name),
                    "--catalog-only",
                    "--config",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(u.file_hash(root / "takeout.zip"), before)
            with closing(
                sqlite3.connect(root / ".pixel-uploader/catalog.sqlite")
            ) as db:
                counts = dict(
                    db.execute("SELECT state,count(*) FROM media GROUP BY state")
                )
                self.assertEqual(counts, {"duplicate": 1, "pending": 1})
            status = json.loads((root / ".pixel-uploader/status.json").read_text())
            self.assertEqual(status["phase"], "Catalog ready")


if __name__ == "__main__":
    unittest.main()
