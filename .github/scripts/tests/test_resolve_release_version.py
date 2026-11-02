from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import resolve_release_version as resolver  # noqa: E402


class ResolveReleaseVersionTest(unittest.TestCase):
    def test_resolves_pack_version_to_minecraft_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Packwiz"
            self._write_pack(root, "1.21.8", "0.6.2", "1.21.8")
            self._write_pack(root, "26.3", "0.7.0", "26.3")
            self._write_pack(root, "26.4", "0.8.0", "26.4")

            self.assertEqual(resolver.find_pack_for_version(root, "0.6.2"), ("1.21.8", "1.21.8"))
            self.assertEqual(resolver.find_pack_for_version(root, "0.8.0"), ("26.4", "26.4"))
            self.assertEqual(resolver.find_pack_for_version(root, "v0.8.0"), ("26.4", "26.4"))
            self.assertEqual(
                resolver.find_pack_for_version(root, "test-0.8.0"), ("26.4", "26.4")
            )

    def test_missing_pack_version_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Packwiz"
            self._write_pack(root, "1.21.8", "0.6.2", "1.21.8")
            with self.assertRaises(FileNotFoundError):
                resolver.find_pack_for_version(root, "9.9.9")

    @staticmethod
    def _write_pack(root: Path, folder: str, version: str, minecraft: str) -> None:
        pack_dir = root / folder
        pack_dir.mkdir(parents=True)
        (pack_dir / "pack.toml").write_text(
            f'name = "DBOP MC {minecraft}"\n'
            f'version = "{version}"\n'
            'pack-format = "packwiz:1.1.0"\n'
            "\n[versions]\n"
            'fabric = "0.20.1"\n'
            f'minecraft = "{minecraft}"\n',
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
