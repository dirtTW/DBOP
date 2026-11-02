from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "sync-packs.py"
spec = importlib.util.spec_from_file_location("sync_packs", SCRIPT)
sync_packs = importlib.util.module_from_spec(spec)
sys.modules["sync_packs"] = sync_packs
spec.loader.exec_module(sync_packs)


PACK_TOML = """name = "DBOP MC 1.21.8"
author = "dirtTW"
version = "{version}"
pack-format = "packwiz:1.1.0"

[index]
file = "index.toml"
hash-format = "sha256"
hash = "abc"

[versions]
fabric = "{fabric}"
minecraft = "1.21.8"
"""


def meta(name: str, filename: str, *, side: str = "both", native: bool = False) -> str:
    if native:
        return (
            f'name = "{name}"\nfilename = "{filename}"\nside = "{side}"\n\n'
            '[download]\nhash-format = "sha1"\nhash = "abc"\nmode = "metadata:curseforge"\n\n'
            "[update]\n[update.curseforge]\nfile-id = 1\nproject-id = 2\n"
        )
    return (
        f'name = "{name}"\nfilename = "{filename}"\nside = "{side}"\n\n'
        '[download]\nurl = "https://example.invalid/x.jar"\n'
        'hash-format = "sha512"\nhash = "abc"\n\n'
        '[update]\n[update.modrinth]\nmod-id = "x"\nversion = "1"\n'
    )


class SyncPacksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pw = self.root / "Packwiz" / "1.21.8"
        self.cf = self.root / "Curseforge" / "1.21.8"
        (self.pw / "mods").mkdir(parents=True)
        (self.cf / "mods").mkdir(parents=True)
        (self.pw / "config").mkdir()
        (self.cf / "config").mkdir()

        (self.pw / "pack.toml").write_text(
            PACK_TOML.format(version="0.6.2", fabric="0.20.1"), encoding="utf-8"
        )
        (self.cf / "pack.toml").write_text(
            PACK_TOML.format(version="0.6.1", fabric="0.17.2"), encoding="utf-8"
        )
        (self.pw / "config" / "shared.json").write_text('{"new": true}\n', encoding="utf-8")
        (self.cf / "config" / "shared.json").write_text('{"old": true}\n', encoding="utf-8")
        (self.cf / "config" / "stale.json").write_text("{}\n", encoding="utf-8")
        (self.pw / "changelog.md").write_text("v0.6.2\n", encoding="utf-8")
        for pack in (self.pw, self.cf):
            (pack / "config" / "isxander-main-menu-credits.json").write_text(
                '{"text":"DBOP v0.6.2"}\n', encoding="utf-8"
            )
            (pack / "config" / "fabric_loader_dependencies.json").write_text(
                '{"overrides":{"fabricloader":{"+depends":{"fabricloader":">=0.17.2"}},'
                '"minecraft":{"+recommends":{"DBOP MC 1.21.8":">0.6.1"}}}}\n',
                encoding="utf-8",
            )

        (self.pw / "mods" / "common.pw.toml").write_text(
            meta("Common Mod", "common-2.jar"), encoding="utf-8"
        )
        (self.cf / "mods" / "common.pw.toml").write_text(
            meta("Common Mod", "common-1.jar"), encoding="utf-8"
        )
        (self.pw / "mods" / "new.pw.toml").write_text(
            meta("New Mod", "new.jar", side="client"), encoding="utf-8"
        )
        (self.pw / "mods" / "server-only.pw.toml").write_text(
            meta("Server Only", "server.jar", side="server"), encoding="utf-8"
        )
        (self.pw / "mods" / "renamed.pw.toml").write_text(
            meta("Server Resource Pack Checker", "checker.jar"), encoding="utf-8"
        )
        (self.cf / "mods" / "server-resourcepack-checker.pw.toml").write_text(
            meta("Server Resourcepack Checker", "checker.jar", native=True), encoding="utf-8"
        )
        (self.cf / "mods" / "native.pw.toml").write_text(
            meta("Native Only", "native.jar", native=True), encoding="utf-8"
        )
        (self.cf / "mods" / "stale.pw.toml").write_text(
            meta("Stale Mod", "stale.jar"), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_sync_updates_both_pack_formats(self) -> None:
        log = sync_packs.sync_version(
            self.root, "1.21.8", update=False, refresh=False, dry_run=False
        )

        cf_pack = (self.cf / "pack.toml").read_text(encoding="utf-8")
        self.assertIn('version = "0.6.2"', cf_pack)
        self.assertIn('fabric = "0.20.1"', cf_pack)

        self.assertEqual(
            (self.cf / "mods" / "common.pw.toml").read_bytes(),
            (self.pw / "mods" / "common.pw.toml").read_bytes(),
        )
        self.assertTrue((self.cf / "mods" / "new.pw.toml").is_file())
        self.assertFalse((self.cf / "mods" / "server-only.pw.toml").exists())
        self.assertTrue((self.cf / "mods" / "native.pw.toml").is_file())
        self.assertFalse((self.cf / "mods" / "stale.pw.toml").exists())
        self.assertTrue((self.cf / "mods" / "server-resourcepack-checker.pw.toml").is_file())

        self.assertEqual(
            (self.cf / "config" / "shared.json").read_text(encoding="utf-8"),
            '{"new": true}\n',
        )
        self.assertFalse((self.cf / "config" / "stale.json").exists())
        self.assertTrue((self.cf / "changelog.md").is_file())
        self.assertFalse((self.cf / "mods" / "renamed.pw.toml").exists())
        self.assertTrue(any("Server Resource Pack Checker" in line for line in log))

    def test_dry_run_does_not_write(self) -> None:
        before = (self.cf / "pack.toml").read_bytes()
        sync_packs.sync_version(
            self.root, "1.21.8", update=False, refresh=False, dry_run=True
        )
        self.assertEqual((self.cf / "pack.toml").read_bytes(), before)
        self.assertFalse((self.cf / "mods" / "new.pw.toml").exists())

    def test_name_normalisation(self) -> None:
        self.assertEqual(
            sync_packs.normalize_name("[ETF] Entity Texture Features - [Fabric & Forge]"),
            "entity texture features",
        )
        self.assertEqual(sync_packs.normalize_name("FerriteCore (Fabric)"), "ferritecore")
        self.assertEqual(
            sync_packs.normalize_name("Server Resource Pack Checker"),
            "server resource pack checker",
        )

    def test_create_new_version_copies_and_bumps(self) -> None:
        log = sync_packs.create_version(
            self.root,
            "1.21.9",
            source_version="1.21.8",
            fabric_loader="0.20.1",
            update=False,
            refresh=False,
        )
        target = self.root / "Packwiz" / "1.21.9"
        target_cf = self.root / "Curseforge" / "1.21.9"
        self.assertTrue((target / "pack.toml").is_file())
        self.assertTrue((target_cf / "pack.toml").is_file())
        pack = (target / "pack.toml").read_text(encoding="utf-8")
        self.assertIn('name = "DBOP MC 1.21.9"', pack)
        self.assertIn('version = "0.6.3"', pack)
        self.assertIn('fabric = "0.20.1"', pack)
        self.assertIn('minecraft = "1.21.9"', pack)
        self.assertTrue((target / "mods" / "common.pw.toml").is_file())

        credits = (target / "config" / "isxander-main-menu-credits.json").read_text(
            encoding="utf-8"
        )
        self.assertIn("v0.6.3", credits)
        deps = (target / "config" / "fabric_loader_dependencies.json").read_text(
            encoding="utf-8"
        )
        self.assertIn('"fabricloader": ">=0.20.1"', deps)
        self.assertIn('"DBOP MC 1.21.9"', deps)
        changelog = (target / "changelog.md").read_text(encoding="utf-8")
        self.assertIn("Minecraft 1.21.9", changelog)
        self.assertTrue(any("Created Packwiz/1.21.9" in line for line in log))

    def test_create_uses_minor_bump_for_new_series(self) -> None:
        self.assertEqual(
            sync_packs.next_pack_version("0.6.2", "1.21.8", "26.3"), "0.7.0"
        )
        self.assertEqual(
            sync_packs.next_pack_version("0.6.2", "1.21.8", "1.21.9"), "0.6.3"
        )

    def test_create_refuses_existing_target_without_force(self) -> None:
        sync_packs.create_version(
            self.root,
            "1.21.9",
            source_version="1.21.8",
            fabric_loader="0.20.1",
            update=False,
            refresh=False,
        )
        with self.assertRaises(FileExistsError):
            sync_packs.create_version(
                self.root,
                "1.21.9",
                source_version="1.21.8",
                fabric_loader="0.20.1",
                update=False,
                refresh=False,
            )


if __name__ == "__main__":
    unittest.main()
