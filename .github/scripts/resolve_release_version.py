#!/usr/bin/env python3
"""Resolve a release tag (pack version) to its Packwiz folder and Minecraft version."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


def find_pack_for_version(pack_root: Path, version: str) -> tuple[str, str]:
    """Return (folder_name, minecraft_version) for a pack version."""
    normalized = version[1:] if version.lower().startswith("v") else version
    for path in sorted(pack_root.glob("*/pack.toml")):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if str(data.get("version", "")) != normalized:
            continue
        minecraft = str((data.get("versions") or {}).get("minecraft") or path.parent.name)
        return path.parent.name, minecraft
    raise FileNotFoundError(
        f"No Packwiz pack with version {version!r} found under {pack_root}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack-root", type=Path, default=Path("Packwiz"))
    parser.add_argument("--version", required=True, help="Release tag / pack version")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)

    try:
        folder, minecraft = find_pack_for_version(args.pack_root, args.version)
    except FileNotFoundError as exc:
        print(f"::error title=Release pack not found::{exc}", file=sys.stderr)
        return 1

    values = {
        "pack_folder": folder,
        "pack_dir": (args.pack_root / folder).as_posix(),
        "minecraft_version": minecraft,
    }
    for key, value in values.items():
        print(f"{key}={value}")
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            for key, value in values.items():
                output.write(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
