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
    def _write_pack(self, root: Path, folder: str, pack_version: str, minecraft: str) -> Path:
        pack_dir = root / "Packwiz" / folder
        (pack_dir / "config").mkdir(parents=True)
        (pack_dir / "pack.toml").write_text(
            f'name = "DBOP MC {minecraft}"\n'
            'author = "dirtTW"\n'
            f'version = "{pack_version}"\n'
            'pack-format = "packwiz:1.1.0"\n'
            "\n[versions]\n"
            'fabric = "0.20.1"\n'
            f'minecraft = "{minecraft}"\n',
            encoding="utf-8",
        )
        (pack_dir / "config" / "fabric_loader_dependencies.json").write_text(
            '{"version": 1, "overrides": {}}\n', encoding="utf-8"
        )
        return pack_dir

    def test_latest_pack_folder_uses_pack_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_pack(root, "1.21.8", "0.6.2", "1.21.8")
            self._write_pack(root, "26.3", "0.7.0", "26.3")
            self._write_pack(root, "26.4", "0.8.0", "26.4")
            latest = prepare.latest_pack_folder(root / "Packwiz")
            self.assertEqual(latest.name, "26.4")

    def test_update_existing_without_version_uses_latest_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_pack(root, "1.21.8", "0.6.2", "1.21.8")
            self._write_pack(root, "26.3", "0.7.0", "26.3")
            self._write_pack(root, "26.4", "0.8.0", "26.4")
            output_path = root / "github-output.txt"
            manifest = {
                "latest": {"release": "26.4"},
                "versions": [
                    {"id": "1.21.8", "type": "release"},
                    {"id": "26.3", "type": "release"},
                    {"id": "26.4", "type": "release"},
                ],
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
                "MINECRAFT_VERSION": "",
                "GITHUB_OUTPUT": str(output_path),
            }
            argv = ["prepare_packwiz_migration.py", "--packwiz-root", str(root / "Packwiz")]
            with patch.object(prepare, "fetch_json", side_effect=fake_fetch), \
                    patch.dict(os.environ, env, clear=False), \
                    patch.object(sys, "argv", argv):
                prepare.main()

            pack = (root / "Packwiz" / "26.4" / "pack.toml").read_text(encoding="utf-8")
            self.assertIn('version = "0.8.1"', pack)
            self.assertIn('minecraft = "26.4"', pack)
            output = output_path.read_text(encoding="utf-8")
            self.assertIn("target_version=26.4", output)
            self.assertIn("target_pack_version=0.8.1", output)

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
