#!/usr/bin/env python3

import argparse
import os
import re
import tomllib
from pathlib import Path


def read_mods(folder: Path) -> dict[str, tuple[str, str, str]]:
    mods = {}
    for metadata_path in sorted(folder.glob("*.pw.toml")):
        metadata = tomllib.loads(metadata_path.read_text(encoding="utf-8"))
        name = metadata.get("name", metadata_path.stem.removesuffix(".pw"))
        filename = metadata.get("filename", metadata_path.name)
        mods[metadata_path.name] = (name, filename, metadata_path.read_text(encoding="utf-8"))
    return mods


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-mods", type=Path, required=True)
    parser.add_argument("--target-mods", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--changelog-output", type=Path, required=True)
    args = parser.parse_args()

    source_mods = read_mods(args.source_mods)
    target_mods = read_mods(args.target_mods)
    updated = []
    unchanged = []

    for metadata_file, (name, old_filename, old_content) in source_mods.items():
        target = target_mods.get(metadata_file)
        if target is None:
            unchanged.append((name, old_filename, "metadata file is missing from target pack"))
        elif target[2] != old_content:
            updated.append((name, old_filename, target[1]))
        else:
            unchanged.append((name, old_filename, "Packwiz did not select a different release"))

    reported_review_items = {}
    log = args.log.read_text(encoding="utf-8", errors="replace")
    for line in log.splitlines():
        no_updater = re.search(r'update system for "(.+)" cannot be found', line)
        failed_check = re.search(r"Failed to check updates for (.+?): (.+)", line)
        pinned = re.search(r"Update skipped for pinned mod (.+)", line)
        if no_updater:
            reported_review_items[no_updater.group(1)] = "no supported update source is configured"
        elif failed_check:
            reported_review_items[failed_check.group(1)] = failed_check.group(2)
        elif pinned:
            reported_review_items[pinned.group(1)] = "updates are pinned"

    version = os.environ.get("MINECRAFT_VERSION", "target version")
    source_version = os.environ.get("SOURCE_VERSION", "source pack")
    source_pack_version = os.environ.get("SOURCE_PACK_VERSION", "0.0.0")
    target_pack_version = os.environ.get("TARGET_PACK_VERSION", "0.0.1")
    exit_code = os.environ.get("PACKWIZ_EXIT_CODE", "unknown")
    lines = [
        f"# Packwiz migration report — Minecraft {version}",
        "",
        f"Source pack: Minecraft {source_version}. Fabric Loader was selected for the target version.",
        "",
        "Packwiz selects releases marked compatible with the target Minecraft version and Fabric. An unchanged mod may already support the target; check its release metadata and test the pack before deciding to replace or disable it.",
        "",
        f"Packwiz command exit code: `{exit_code}`.",
        "",
        f"## Updated ({len(updated)})",
        "",
    ]
    if updated:
        lines.extend(f"- **{name}**: `{old}` → `{new}`" for name, old, new in updated)
    else:
        lines.append("- No mod metadata changed to a different release.")

    lines.extend(["", f"## Not updated — review for alternatives or disabling ({len(unchanged)})", ""])
    if unchanged:
        for name, filename, default_reason in unchanged:
            reason = reported_review_items.get(name, default_reason)
            lines.append(f"- **{name}** (`{filename}`) — {reason}")
    else:
        lines.append("- Every source mod selected a different release.")

    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")

    changelog_lines = [
        f"**Changelog MC {version}**",
        "",
        f"### **v{target_pack_version}**",
        "",
        "- Update mods",
    ]
    if updated:
        changelog_lines.extend(f"    - {name}" for name, _, _ in updated)
    else:
        changelog_lines.append("    - No mod updates")
    changelog_lines.extend(
        [
            "",
            f"**Full Changelog**: [https://github.com/dirtTW/DBOP/compare/{source_pack_version}...{target_pack_version}](https://github.com/dirtTW/DBOP/compare/{source_pack_version}...{target_pack_version})",
            "",
        ]
    )
    args.changelog_output.write_text("\n".join(changelog_lines), encoding="utf-8")


if __name__ == "__main__":
    main()
