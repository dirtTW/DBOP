#!/usr/bin/env python3
"""Extract a Packwiz changelog section and count its updated mods."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

VERSION_HEADING = re.compile(r"^### \*\*v?([^*]+)\*\*\s*$", re.MULTILINE)


def normalize_version(version: str) -> str:
    """Strip an optional ``v`` prefix and a ``test-`` release tag prefix."""
    normalized = version[1:] if version.lower().startswith("v") else version
    if normalized.lower().startswith("test-"):
        normalized = normalized[5:]
    return normalized


def extract_section(changelog: str, version: str) -> str:
    """Return the changelog body for one pack version."""
    normalized = normalize_version(version)
    matches = list(VERSION_HEADING.finditer(changelog))
    for index, match in enumerate(matches):
        if match.group(1).strip() != normalized:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(changelog)
        return changelog[match.end() : end].strip()
    return ""


def count_mod_updates(section: str) -> int:
    """Count the indented mod bullets under the changelog's Update mods list."""
    return sum(
        1
        for line in section.splitlines()
        if re.match(r"^\s{2,}-\s+\S", line)
    )


def build_notes(changelog: str, version: str, minecraft: str) -> tuple[str, int]:
    normalized = normalize_version(version)
    section = extract_section(changelog, version)
    count = count_mod_updates(section)
    header = f"## **Changelog MC {minecraft}**\n\n"
    heading = f"### **v{normalized}**\n\n"
    body = heading + section if section else heading + "- No changelog section found."
    return header + body + "\n", count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changelog", required=True, type=Path)
    parser.add_argument("--version", required=True, help="Release tag / pack version")
    parser.add_argument("--minecraft", default="", help="Minecraft version for the notes header")
    parser.add_argument("--count-only", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    changelog = (
        args.changelog.read_text(encoding="utf-8-sig")
        if args.changelog.is_file()
        else ""
    )
    notes, count = build_notes(changelog, args.version, args.minecraft)
    if args.count_only:
        print(count)
        return 0
    print(f"mod_updates={count}")
    if args.output:
        args.output.write_text(notes, encoding="utf-8")
        print(f"notes_file={args.output}")
    else:
        print(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
