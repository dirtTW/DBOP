from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import analyze_minecraft_logs as analyzer  # noqa: E402


class AnalyzerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run_dir = self.root / "run"
        self.pack_dir = self.root / "Packwiz" / "1.21.8"
        self.hmc_dir = self.root / "HeadlessMC"
        (self.run_dir / "mods").mkdir(parents=True)
        (self.run_dir / "logs").mkdir()
        (self.run_dir / "crash-reports").mkdir()
        (self.pack_dir / "mods").mkdir(parents=True)
        (self.hmc_dir / "logs").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    # -- helpers ---------------------------------------------------------

    def add_mod(
        self,
        jar_name: str,
        mod_id: str,
        name: str,
        *,
        classes: tuple[str, ...] = (),
        mixin_config: dict | None = None,
        mixin_file: str | None = None,
        entrypoint: str | None = None,
    ) -> Path:
        jar_path = self.run_dir / "mods" / jar_name
        meta = {
            "schemaVersion": 1,
            "id": mod_id,
            "name": name,
            "version": "1.0.0",
        }
        if entrypoint:
            meta["entrypoints"] = {"client": [entrypoint]}
        if mixin_file:
            meta["mixins"] = [mixin_file]
        with zipfile.ZipFile(jar_path, "w") as archive:
            archive.writestr("fabric.mod.json", json.dumps(meta))
            if mixin_file and mixin_config is not None:
                archive.writestr(mixin_file, json.dumps(mixin_config))
            for class_name in classes:
                archive.writestr(class_name.replace(".", "/") + ".class", b"\xca\xfe\xba\xbe")

        meta_path = self.pack_dir / "mods" / f"{mod_id}.pw.toml"
        meta_path.write_text(
            f'name = "{name}"\nfilename = "{jar_name}"\nside = "client"\n', encoding="utf-8"
        )
        return jar_path

    def write_crash(self, text: str) -> Path:
        path = self.run_dir / "crash-reports" / "crash-2026-11-02_12.00.00-client.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def write_log(self, text: str) -> Path:
        path = self.run_dir / "logs" / "latest.log"
        path.write_text(text, encoding="utf-8")
        return path

    def analyze(self, *, strict: bool = True, game_outcome: str = "success") -> analyzer.Analysis:
        return analyzer.analyze(
            run_dir=self.run_dir,
            hmc_dir=self.hmc_dir,
            pack_dir=self.pack_dir,
            game_outcome=game_outcome,
            strict=strict,
            minecraft_version="1.21.8",
        )

    # -- tests -----------------------------------------------------------

    def test_crash_report_identifies_mixin_mod(self) -> None:
        self.add_mod(
            "chat_heads-0.14.0-fabric-1.21.6.jar",
            "chat_heads",
            "Chat Heads",
            classes=("com.example.chatheads.ChatHeads", "com.example.chatheads.mixin.ChatMixin"),
            mixin_file="chat_heads.mixins.json",
            mixin_config={
                "required": True,
                "package": "com.example.chatheads.mixin",
                "client": ["ChatMixin"],
            },
            entrypoint="com.example.chatheads.ChatHeads",
        )
        self.run_dir.joinpath("logs", "latest.log").write_text(
            "[Render thread/INFO]: Loading Minecraft 1.21.8 with Fabric Loader 0.17.2\n",
            encoding="utf-8",
        )
        self.write_crash(
            "---- Minecraft Crash Report ----\n"
            "// Oops\n"
            "Description: Rendering screen\n\n"
            "java.lang.NoSuchMethodError: 'void net.minecraft.class_310.method_1()'\n"
            "\tat com.example.chatheads.mixin.ChatMixin.render(ChatMixin.java:42)\n"
            "\tat net.minecraft.class_310.method_1(class_310.java:100)\n"
            "Caused by: org.spongepowered.asm.mixin.transformer.throwables.MixinTransformerError: "
            "Mixin apply failed chat_heads.mixins.json:ChatMixin from mod chat_heads failed\n"
            "\tat org.spongepowered.asm.mixin.transformer.MixinProcessor.applyMixins(MixinProcessor.java:1)\n"
            "\tat com.example.chatheads.ChatHeads.init(ChatHeads.java:10)\n"
        )

        result = self.analyze()

        self.assertFalse(result.passed)
        self.assertEqual(result.status, "crash")
        self.assertEqual(result.description, "Rendering screen")
        self.assertTrue(result.suspects, "expected at least one suspected mod")
        top = result.suspects[0]
        self.assertEqual(top["id"], "chat_heads")
        self.assertEqual(top["name"], "Chat Heads")
        self.assertEqual(top["jar"], "chat_heads-0.14.0-fabric-1.21.6.jar")
        self.assertTrue(top["pw_toml"].endswith("chat_heads.pw.toml"))
        self.assertGreater(top["score"], 0)

    def test_native_crash_hs_err_is_identified(self) -> None:
        self.add_mod(
            "chat_heads.jar",
            "chat_heads",
            "Chat Heads",
            classes=("com.example.chatheads.ChatHeads",),
        )
        self.write_log(
            "[Render thread/INFO]: Loading Minecraft 1.21.8 with Fabric Loader 0.17.2\n"
        )
        (self.run_dir / "hs_err_pid1234.log").write_text(
            "# A fatal error has been detected by the Java Runtime Environment\n"
            "Java frames: (J=compiled Java code, j=interpreted, Vv=VM code)\n"
            "J 123 c1 com.example.chatheads.ChatHeads.render(Lnet/minecraft/class_310;)V\n"
            "j 45 com.example.chatheads.ChatHeads.tick()V\n",
            encoding="utf-8",
        )
        result = self.analyze()
        self.assertFalse(result.passed)
        self.assertEqual(result.status, "crash")
        self.assertTrue(result.suspects)
        self.assertEqual(result.suspects[0]["id"], "chat_heads")

    def test_fabric_loader_incompatible_mod_is_identified(self) -> None:
        self.add_mod(
            "morechathistory-1.3.1.jar",
            "morechathistory",
            "More Chat History",
            classes=("com.example.morechathistory.MoreChatHistory",),
        )
        self.write_log(
            "[main/INFO]: Loading Minecraft 1.21.8 with Fabric Loader 0.17.2\n"
            "[main/ERROR]: Incompatible mods found!\n"
            "net.fabricmc.loader.impl.FormattedException: Some of your mods are incompatible with the game or each other!\n"
            "Mod 'More Chat History' (morechathistory) 1.3.1 requires version 1.21.9 of minecraft, which is missing!\n"
            "\tat net.fabricmc.loader.impl.FormattedException.ofLocalized(FormattedException.java:1)\n"
        )

        result = self.analyze()

        self.assertFalse(result.passed)
        self.assertEqual(result.status, "error")
        self.assertTrue(result.suspects)
        self.assertEqual(result.suspects[0]["id"], "morechathistory")
        self.assertTrue(any("Incompatible" in finding.line for finding in result.findings))

    def test_clean_log_passes(self) -> None:
        self.add_mod("sodium.jar", "sodium", "Sodium")
        self.write_log(
            "[Render thread/INFO]: Loading Minecraft 1.21.8 with Fabric Loader 0.17.2\n"
            "[Render thread/INFO]: Backend library: LWJGL version 3.3.6\n"
            "[Render thread/INFO]: Stopping!\n"
        )
        (self.hmc_dir / "logs" / "headlessmc.log").write_text(
            "Initializing Crash Report Watcher for C:/tmp/run\n", encoding="utf-8"
        )

        result = self.analyze()

        self.assertTrue(result.passed, result.reason)
        self.assertEqual(result.status, "pass")
        self.assertFalse(result.suspects)
        self.assertFalse(
            any("crash report" in finding.message.lower() for finding in result.findings)
        )

    def test_sodium_lwjgl_startup_failure_is_attributed(self) -> None:
        self.add_mod(
            "sodium-fabric-0.7.0+mc1.21.8.jar",
            "sodium",
            "Sodium",
            classes=("net.caffeinemc.mods.sodium.client.SodiumClientMod",),
        )
        self.write_log(
            "[Render thread/INFO]: Loading Minecraft 1.21.8 with Fabric Loader 0.17.2\n"
            "[Render thread/ERROR]: The game failed to start because the currently active LWJGL "
            "version is not compatible.\n"
            "Installed version: \n"
            "Required version: 3.3.3\n"
            "For more information, please see: "
            "https://link.caffeinemc.net/help/sodium/runtime-issue/lwjgl3/gh-2561\n"
        )
        result = self.analyze(game_outcome="failure")
        self.assertFalse(result.passed)
        self.assertEqual(result.status, "error")
        self.assertTrue(result.suspects)
        self.assertEqual(result.suspects[0]["id"], "sodium")

    def test_non_fatal_exception_is_attributed_as_warning(self) -> None:
        self.add_mod(
            "IMBlocker-5.4.4-fabric+1.17-1.21.8.jar",
            "imblocker",
            "IMBlocker",
            classes=(
                "io.github.reserveword.imblocker.common.LinuxIMFramework",
                "io.github.reserveword.imblocker.common.IMManagerLinux",
            ),
        )
        self.write_log(
            "[04:50:20] [main/INFO]: Loading Minecraft 1.21.8 with Fabric Loader 0.17.2\n"
            '[04:50:43] [Render thread/INFO]: [STDERR]: java.io.IOException: Cannot run program '
            '"ibus": Exec failed, error: 2 (No such file or directory)\n'
            "[04:50:43] [Render thread/INFO]: [STDERR]: \tat java.base/java.lang.ProcessBuilder.start(ProcessBuilder.java:1170)\n"
            "[04:50:43] [Render thread/INFO]: [STDERR]: \tat knot//io.github.reserveword.imblocker.common.LinuxIMFramework.setState(LinuxIMFramework.java:52)\n"
            "[04:50:43] [Render thread/INFO]: [STDERR]: \tat knot//io.github.reserveword.imblocker.common.IMManagerLinux.setState(IMManagerLinux.java:15)\n"
            "[04:50:43] [Render thread/INFO]: [STDERR]: Caused by: java.io.IOException: Exec failed, error: 2 (No such file or directory)\n"
            "[04:50:43] [Render thread/INFO]: [STDERR]: \tat knot//io.github.reserveword.imblocker.common.LinuxIMFramework.setState(LinuxIMFramework.java:52)\n"
            "[04:51:21] [Render thread/INFO]: Stopping!\n"
        )

        result = self.analyze()

        self.assertTrue(result.passed, result.reason)
        self.assertEqual(result.status, "pass")
        self.assertEqual(len(result.observed_errors), 1, "Caused by duplicates should be skipped")
        top = result.observed_errors[0]["suspects"][0]
        self.assertEqual(top["id"], "imblocker")
        self.assertIn("Cannot run program", result.observed_errors[0]["message"])
        self.assertTrue(
            any("non-fatal exception" in finding.message for finding in result.findings)
        )

    def test_knot_prefixed_stack_frames_are_attributed(self) -> None:
        self.add_mod(
            "chat_heads.jar",
            "chat_heads",
            "Chat Heads",
            classes=("com.example.chatheads.ChatHeads",),
        )
        self.write_crash(
            "---- Minecraft Crash Report ----\n"
            "Description: Rendering screen\n\n"
            "java.lang.NoSuchMethodError: 'void net.minecraft.class_310.method_1()'\n"
            "\tat knot//com.example.chatheads.ChatHeads.render(ChatHeads.java:42)\n"
            "\tat net.minecraft.class_310.method_1(class_310.java:100)\n"
        )
        result = self.analyze()
        self.assertFalse(result.passed)
        self.assertTrue(result.suspects)
        self.assertEqual(result.suspects[0]["id"], "chat_heads")

    def test_optional_class_probe_at_warn_is_not_a_failure(self) -> None:
        self.add_mod("sodium.jar", "sodium", "Sodium")
        self.write_log(
            "[Render thread/INFO]: Loading Minecraft 1.21.8 with Fabric Loader 0.17.2\n"
            "[Render thread/WARN]: Error loading class: me/jellysquid/mods/sodium/client/gui/"
            "SodiumGameOptionPages (java.lang.ClassNotFoundException: "
            "me/jellysquid/mods/sodium/client/gui/SodiumGameOptionPages)\n"
            "[Render thread/INFO]: Stopping!\n"
        )
        result = self.analyze()
        self.assertTrue(result.passed, result.reason)
        self.assertFalse(result.observed_errors)

    def test_missing_log_is_reported_as_did_not_start(self) -> None:
        result = self.analyze()
        self.assertFalse(result.passed)
        self.assertEqual(result.status, "did_not_start")

    def test_failed_game_step_fails_even_without_crash(self) -> None:
        self.write_log(
            "[Render thread/INFO]: Loading Minecraft 1.21.8 with Fabric Loader 0.17.2\n"
            "[Render thread/INFO]: Stopping!\n"
        )
        result = self.analyze(game_outcome="failure")
        self.assertFalse(result.passed)
        self.assertEqual(result.status, "game_failed")

    def test_nested_fabric_api_module_is_attributed(self) -> None:
        outer = self.run_dir / "mods" / "fabric-api-0.134.0+1.21.8.jar"
        nested_meta = {
            "schemaVersion": 1,
            "id": "fabric-networking-api-v1",
            "name": "Fabric Networking API (v1)",
            "version": "1.0.0",
        }
        nested_buffer = io.BytesIO()
        with zipfile.ZipFile(nested_buffer, "w") as nested:
            nested.writestr("fabric.mod.json", json.dumps(nested_meta))
            nested.writestr(
                "net/fabricmc/fabric/impl/networking/Broken.class", b"\xca\xfe\xba\xbe"
            )
        with zipfile.ZipFile(outer, "w") as archive:
            archive.writestr(
                "fabric.mod.json",
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "id": "fabric-api",
                        "name": "Fabric API",
                        "version": "1.0.0",
                    }
                ),
            )
            archive.writestr(
                "META-INF/jars/fabric-networking-api-v1.jar", nested_buffer.getvalue()
            )

        self.write_log(
            "[Render thread/INFO]: Loading Minecraft 1.21.8 with Fabric Loader 0.17.2\n"
            "[Render thread/ERROR]: java.lang.RuntimeException: boom\n"
            "\tat net.fabricmc.fabric.impl.networking.Broken.method(Broken.java:1)\n"
            "[Render thread/INFO]: Stopping!\n"
        )

        result = self.analyze(strict=False)
        # The custom RuntimeException is not one of the fatal patterns, so this
        # run is a pass; the important part is that class->mod resolution works.
        index = analyzer.build_index(self.run_dir, self.pack_dir)
        keys, kind = index.match_class("net.fabricmc.fabric.impl.networking.Broken")
        self.assertIn("fabric-networking-api-v1", keys)
        self.assertEqual(kind, "class")

    def test_json_and_markdown_report(self) -> None:
        self.add_mod(
            "chat_heads.jar",
            "chat_heads",
            "Chat Heads",
            classes=("com.example.chatheads.ChatHeads",),
        )
        self.write_crash(
            "---- Minecraft Crash Report ----\n"
            "Description: Rendering screen\n\n"
            "java.lang.NoSuchMethodError: 'void net.minecraft.class_310.method_1()'\n"
            "\tat com.example.chatheads.ChatHeads.render(ChatHeads.java:42)\n"
        )
        result = self.analyze()
        markdown = analyzer.render_markdown(result)
        self.assertIn("Suspected mod(s)", markdown)
        self.assertIn("Chat Heads", markdown)
        self.assertIn("chat_heads", markdown)


if __name__ == "__main__":
    unittest.main()
