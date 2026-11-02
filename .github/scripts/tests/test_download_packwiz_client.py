from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS.parents[1]
sys.path.insert(0, str(SCRIPTS))

import download_packwiz_client as downloader  # noqa: E402


class DownloadPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pack_dir = REPO_ROOT / "Packwiz" / "1.21.8"

    def test_plan_contains_client_mods_and_skips_server_mods(self) -> None:
        plans, skipped, pack = downloader.load_plan(self.pack_dir, Path("run"), client_only=True)

        self.assertEqual(pack["minecraft"], "1.21.8")
        self.assertEqual(pack["fabric"], "0.17.2")

        mods = [plan for plan in plans if plan.kind == "download" and plan.destination.startswith("mods/")]
        self.assertGreaterEqual(len(mods), 50)
        self.assertTrue(all(plan.url and plan.digest for plan in mods))
        self.assertTrue(all(plan.hash_format in downloader.HASH_NAMES for plan in mods))

        skipped_names = {plan.filename for plan in skipped}
        self.assertIn("e4mc_minecraft-fabric-5.4.1.jar", skipped_names)

        chat_heads = next(plan for plan in mods if plan.name == "Chat Heads")
        self.assertEqual(chat_heads.filename, "chat_heads-0.14.0-fabric-1.21.6.jar")
        self.assertEqual(chat_heads.destination, "mods/chat_heads-0.14.0-fabric-1.21.6.jar")
        self.assertEqual(chat_heads.hash_format, "sha512")

    def test_overrides_are_copied_from_the_pack(self) -> None:
        plans, _, _ = downloader.load_plan(self.pack_dir, Path("run"), client_only=True)
        overrides = [plan for plan in plans if plan.kind == "override"]
        self.assertTrue(any(plan.destination == "config/fabric_loader_dependencies.json" for plan in overrides))
        self.assertTrue(any(plan.destination == "config/yosbr/config/sodium-options.json" for plan in overrides))

    def test_exclude_filter(self) -> None:
        plans, skipped, _ = downloader.load_plan(
            self.pack_dir,
            Path("run"),
            client_only=True,
            exclude=["chat.heads"],
        )
        self.assertFalse(any(plan.name == "Chat Heads" for plan in plans))
        self.assertTrue(any(plan.name == "Chat Heads" for plan in skipped))

    def test_override_is_copied_from_the_checked_out_pack(self) -> None:
        plans, _, _ = downloader.load_plan(self.pack_dir, Path("run"), client_only=True)
        override = next(
            plan for plan in plans
            if plan.kind == "override" and plan.destination == "config/fabric_loader_dependencies.json"
        )
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            status = downloader.stage_file(
                override, self.pack_dir, dest, dest / "cache", dry_run=False
            )
            self.assertIn(status, {"copied", "cached"})
            target = dest / override.destination
            self.assertTrue(target.is_file())
            self.assertEqual(
                target.read_bytes(),
                (self.pack_dir / override.source).read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
