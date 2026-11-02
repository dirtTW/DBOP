from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import prepare_packwiz_migration as prepare  # noqa: E402


class PrepareMigrationTest(unittest.TestCase):
    def test_latest_stable_fabric_loader_skips_unstable_builds(self) -> None:
        payload = [
            {"loader": {"version": "0.20.1", "stable": True}},
            {"loader": {"version": "0.21.0", "stable": False}},
        ]
        with patch.object(prepare, "fetch_json", return_value=payload):
            self.assertEqual(prepare.latest_stable_fabric_loader("1.21.8"), "0.20.1")

    def test_update_existing_pack_bumps_fabric_loader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack_dir = root / "Packwiz" / "1.21.8"
            (pack_dir / "config").mkdir(parents=True)
            (pack_dir / "pack.toml").write_text(
                'name = "DBOP MC 1.21.8"\n'
                'author = "dirtTW"\n'
                'version = "0.6.1"\n'
                'pack-format = "packwiz:1.1.0"\n'
                "\n"
                "[index]\n"
                'file = "index.toml"\n'
                'hash-format = "sha256"\n'
                'hash = "abc"\n'
                "\n"
                "[versions]\n"
                'fabric = "0.17.2"\n'
                'minecraft = "1.21.8"\n',
                encoding="utf-8",
            )
            (pack_dir / "config" / "fabric_loader_dependencies.json").write_text(
                '{\n  "version": 1,\n  "overrides": {\n'
                '    "fabricloader": {\n      "+depends": {\n'
                '        "fabricloader": ">=0.17.2"\n      }\n    }\n  }\n}\n',
                encoding="utf-8",
            )
            output_path = root / "github-output.txt"

            manifest = {
                "latest": {"release": "1.21.8"},
                "versions": [{"id": "1.21.8", "type": "release"}],
            }
            loaders = [{"loader": {"version": "0.20.1", "stable": True}}]

            def fake_fetch(url: str) -> object:
                if "version_manifest" in url:
                    return manifest
                if "meta.fabricmc.net" in url:
                    return loaders
                raise AssertionError(url)

            env = {
                "MODE": "update_existing",
                "MINECRAFT_VERSION": "1.21.8",
                "GITHUB_OUTPUT": str(output_path),
            }
            argv = ["prepare_packwiz_migration.py", "--packwiz-root", str(root / "Packwiz")]
            with patch.object(prepare, "fetch_json", side_effect=fake_fetch), \
                    patch.dict(os.environ, env, clear=False), \
                    patch.object(sys, "argv", argv):
                prepare.main()

            pack = (pack_dir / "pack.toml").read_text(encoding="utf-8")
            self.assertIn('version = "0.6.2"', pack)
            self.assertIn('fabric = "0.20.1"', pack)
            self.assertIn('minecraft = "1.21.8"', pack)

            dependencies = (pack_dir / "config" / "fabric_loader_dependencies.json").read_text(
                encoding="utf-8"
            )
            self.assertIn('"fabricloader": ">=0.20.1"', dependencies)
            self.assertIn("fabric_loader=0.20.1", output_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
