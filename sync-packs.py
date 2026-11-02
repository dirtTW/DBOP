#!/usr/bin/env python3
"""Keep a Packwiz folder and its CurseForge folder in sync.

Usage (Windows)::

    python sync-packs.py 1.21.8
    python sync-packs.py 1.21.8 --no-update
    python sync-packs.py 1.21.8 --dry-run
    python sync-packs.py --all

What it does for one Minecraft version:

1. ``packwiz update --all`` in ``Packwiz/<version>`` (unless ``--no-update``).
2. Copies the Packwiz pack version, Fabric loader, Minecraft version and
   config files into ``Curseforge/<version>``.
3. Syncs mod/resource-pack metadata:
   - Mods already managed by CurseForge keep their CurseForge project/file IDs
     and are updated by CurseForge's own update API.
   - Mods that only exist in Packwiz are copied into the CurseForge folder as
     external files, so ``packwiz curseforge export`` bundles their jars in the
     CurseForge zip.
   - External files that were removed from Packwiz are removed from CurseForge.
   - CurseForge-only native mods are always kept.
4. ``packwiz update --all`` in ``Curseforge/<version>``, then refresh both
   packs (unless ``--no-update`` / ``--no-refresh``).

The script never edits mods in place outside the two pack folders. Commit the
result afterwards so both pack formats stay in one change.
"""

from __future__ import annotations

import argparse
import difflib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

ROOT = Path(__file__).resolve().parent
VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+){1,2}$")


@dataclass
class Meta:
    path: Path
    data: dict
    raw: bytes
    name: str
    filename: str
    side: str

    @property
    def native_curseforge(self) -> bool:
        update = self.data.get("update") or {}
        download = self.data.get("download") or {}
        return "curseforge" in update or download.get("mode") == "metadata:curseforge"


def read_meta(path: Path) -> Meta:
    raw = path.read_bytes()
    data = tomllib.loads(raw.decode("utf-8-sig"))
    return Meta(
        path=path,
        data=data,
        raw=raw,
        name=str(data.get("name") or path.stem),
        filename=str(data.get("filename") or ""),
        side=str(data.get("side") or "both"),
    )


def normalize_name(value: str) -> str:
    """Normalise mod names so CurseForge/Modrinth spellings can be compared."""
    value = value.lower()
    value = re.sub(r"\[[^\]]*\]", " ", value)
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"[-_/]+", " ", value)
    value = re.sub(r"\b(fabric|forge|neoforge|port|edition)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def load_aliases(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    aliases = data.get("aliases") or {}
    return {str(key).lower(): str(value) for key, value in aliases.items()}


def match_meta(
    packwiz: Meta,
    curseforge: list[Meta],
    used: set[Path],
    aliases: dict[str, str],
) -> tuple[Meta | None, str]:
    available = [item for item in curseforge if item.path not in used]
    by_meta_name = {item.path.name.lower(): item for item in available}
    by_name = {item.name.lower(): item for item in available}
    by_norm: dict[str, list[Meta]] = {}
    for item in available:
        by_norm.setdefault(normalize_name(item.name), []).append(item)

    for key in (packwiz.path.name, packwiz.name):
        target = aliases.get(key.lower())
        if target:
            target_lower = target.lower()
            for item in available:
                if item.path.name.lower() == target_lower or item.name.lower() == target_lower:
                    return item, "alias"

    if packwiz.path.name.lower() in by_meta_name:
        return by_meta_name[packwiz.path.name.lower()], "meta filename"
    if packwiz.name.lower() in by_name:
        return by_name[packwiz.name.lower()], "exact name"

    normalized = normalize_name(packwiz.name)
    if normalized in by_norm and len(by_norm[normalized]) == 1:
        return by_norm[normalized][0], "normalised name"

    best: tuple[float, Meta | None] = (0.0, None)
    for item in available:
        ratio = difflib.SequenceMatcher(None, normalized, normalize_name(item.name)).ratio()
        if ratio > best[0]:
            best = (ratio, item)
    if best[1] is not None and best[0] >= 0.92:
        return best[1], f"fuzzy name ({best[0]:.2f})"
    return None, ""


def replace_pack_field(contents: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}\s*=\s*.*$", re.MULTILINE)
    updated, count = pattern.subn(f'{key} = "{value}"', contents)
    if count != 1:
        raise ValueError(f"Expected one {key!r} field in pack.toml, found {count}")
    return updated


def read_pack_versions(pack_dir: Path) -> dict[str, str]:
    data = tomllib.loads((pack_dir / "pack.toml").read_text(encoding="utf-8-sig"))
    return {
        "name": str(data.get("name", "")),
        "version": str(data.get("version", "")),
        "fabric": str((data.get("versions") or {}).get("fabric", "")),
        "minecraft": str((data.get("versions") or {}).get("minecraft", "")),
    }


def sync_pack_metadata(packwiz_dir: Path, curseforge_dir: Path, dry_run: bool, log: list[str]) -> None:
    source = read_pack_versions(packwiz_dir)
    target_path = curseforge_dir / "pack.toml"
    contents = target_path.read_text(encoding="utf-8-sig")
    for field, value in source.items():
        if field and value:
            contents = replace_pack_field(contents, field, value)
    if not dry_run:
        target_path.write_text(contents, encoding="utf-8")
    log.append(
        f"pack.toml -> {source['name']} v{source['version']} "
        f"(Fabric {source['fabric']}, MC {source['minecraft']})"
    )


def mirror_directory(source: Path, target: Path, dry_run: bool, log: list[str]) -> None:
    """Copy source files over target and remove target-only files."""
    if not source.is_dir():
        return
    target.mkdir(parents=True, exist_ok=True)
    source_files = {
        path.relative_to(source).as_posix(): path for path in source.rglob("*") if path.is_file()
    }
    target_files = {
        path.relative_to(target).as_posix(): path for path in target.rglob("*") if path.is_file()
    }
    for relative, source_path in source_files.items():
        target_path = target / relative
        if target_path.is_file() and target_path.read_bytes() == source_path.read_bytes():
            continue
        log.append(f"{target.name}/{relative} <- Packwiz/{source.name}/{relative}")
        if not dry_run:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
    for relative, target_path in target_files.items():
        if relative not in source_files:
            log.append(f"remove stale {target.name}/{relative}")
            if not dry_run:
                target_path.unlink()


def sync_meta_folder(
    packwiz_dir: Path,
    curseforge_dir: Path,
    folder: str,
    aliases: dict[str, str],
    dry_run: bool,
    keep_extra: bool,
    log: list[str],
) -> None:
    packwiz_folder = packwiz_dir / folder
    curseforge_folder = curseforge_dir / folder
    if not packwiz_folder.is_dir():
        return
    curseforge_folder.mkdir(parents=True, exist_ok=True)

    packwiz_metas = [read_meta(path) for path in sorted(packwiz_folder.glob("*.pw.toml"))]
    curseforge_metas = [read_meta(path) for path in sorted(curseforge_folder.glob("*.pw.toml"))]
    used: set[Path] = set()
    kept_native = 0
    copied = 0
    updated = 0

    for packwiz in packwiz_metas:
        match, reason = match_meta(packwiz, curseforge_metas, used, aliases)
        if match is not None:
            used.add(match.path)
            if match.native_curseforge:
                kept_native += 1
                log.append(
                    f"keep CurseForge-native {folder}/{match.path.name} "
                    f"for '{packwiz.name}' ({reason})"
                )
                continue
            if match.raw != packwiz.raw:
                updated += 1
                log.append(f"update external {folder}/{match.path.name} <- Packwiz/{packwiz.path.name}")
                if not dry_run:
                    match.path.write_bytes(packwiz.raw)
            continue

        if packwiz.side == "server":
            log.append(f"skip server-only {folder}/{packwiz.path.name} (no CurseForge match)")
            continue

        destination = curseforge_folder / packwiz.path.name
        if destination.exists():
            existing = read_meta(destination)
            if existing.native_curseforge:
                used.add(destination)
                log.append(f"keep CurseForge-native {folder}/{destination.name} (same filename)")
                kept_native += 1
                continue
            used.add(destination)
            if existing.raw != packwiz.raw:
                updated += 1
                log.append(f"update external {folder}/{destination.name} <- Packwiz/{packwiz.path.name}")
                if not dry_run:
                    destination.write_bytes(packwiz.raw)
            continue

        copied += 1
        used.add(destination)
        log.append(f"add external {folder}/{destination.name} <- Packwiz/{packwiz.path.name}")
        if not dry_run:
            destination.write_bytes(packwiz.raw)

    for curseforge in curseforge_metas:
        if curseforge.path in used or curseforge.native_curseforge:
            continue
        if keep_extra:
            log.append(f"keep extra external {folder}/{curseforge.path.name}")
            continue
        log.append(f"remove stale external {folder}/{curseforge.path.name}")
        if not dry_run:
            curseforge.path.unlink()

    log.append(
        f"{folder}: {len(packwiz_metas)} Packwiz file(s), "
        f"{kept_native} CurseForge-native kept, {copied} added, {updated} updated"
    )


def run_packwiz(packwiz: str, cwd: Path, args: list[str], log: list[str]) -> None:
    command = [packwiz, "--yes", *args]
    log.append(f"[{cwd.parent.name}/{cwd.name}] {' '.join(command)}")
    result = subprocess.run(command, cwd=cwd, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"packwiz {' '.join(args)} failed in {cwd} (exit {result.returncode})")


def sync_version(
    root: Path,
    version: str,
    *,
    packwiz: str = "packwiz",
    update: bool = True,
    refresh: bool = True,
    dry_run: bool = False,
    keep_extra: bool = False,
    aliases: dict[str, str] | None = None,
) -> list[str]:
    packwiz_dir = root / "Packwiz" / version
    curseforge_dir = root / "Curseforge" / version
    if not (packwiz_dir / "pack.toml").is_file():
        raise FileNotFoundError(f"Packwiz/{version}/pack.toml not found")
    if not (curseforge_dir / "pack.toml").is_file():
        raise FileNotFoundError(f"Curseforge/{version}/pack.toml not found")

    aliases = aliases or {}
    log: list[str] = [f"== Minecraft {version} =="]

    if update and not dry_run:
        run_packwiz(packwiz, packwiz_dir, ["update", "--all"], log)

    sync_pack_metadata(packwiz_dir, curseforge_dir, dry_run, log)
    mirror_directory(packwiz_dir / "config", curseforge_dir / "config", dry_run, log)

    for folder in ("mods", "resourcepacks"):
        sync_meta_folder(
            packwiz_dir,
            curseforge_dir,
            folder,
            aliases,
            dry_run,
            keep_extra,
            log,
        )

    changelog = packwiz_dir / "changelog.md"
    if changelog.is_file():
        target = curseforge_dir / "changelog.md"
        if not target.is_file() or target.read_bytes() != changelog.read_bytes():
            log.append("changelog.md <- Packwiz")
            if not dry_run:
                shutil.copy2(changelog, target)

    if update and not dry_run:
        run_packwiz(packwiz, curseforge_dir, ["update", "--all"], log)
    if refresh and not dry_run:
        run_packwiz(packwiz, packwiz_dir, ["refresh"], log)
        run_packwiz(packwiz, curseforge_dir, ["refresh"], log)

    return log


def available_versions(root: Path) -> list[str]:
    packwiz_root = root / "Packwiz"
    versions = [
        path.name
        for path in packwiz_root.iterdir()
        if path.is_dir()
        and VERSION_PATTERN.fullmatch(path.name)
        and (path / "pack.toml").is_file()
        and (root / "Curseforge" / path.name / "pack.toml").is_file()
    ]

    def sort_key(value: str) -> tuple[int, ...]:
        return tuple(int(part) for part in value.split("."))

    return sorted(versions, key=sort_key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", nargs="?", help="Minecraft version, e.g. 1.21.8 (default: latest)")
    parser.add_argument("--all", action="store_true", help="Process every version that has both pack folders")
    parser.add_argument("--packwiz", default="packwiz", help="packwiz executable (default: packwiz)")
    parser.add_argument("--no-update", action="store_true", help="Do not run packwiz update --all")
    parser.add_argument("--no-refresh", action="store_true", help="Do not run packwiz refresh")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing files")
    parser.add_argument(
        "--keep-extra",
        action="store_true",
        help="Do not remove external CurseForge files that no longer exist in Packwiz",
    )
    parser.add_argument(
        "--aliases",
        type=Path,
        default=ROOT / "pack-sync-aliases.toml",
        help="Optional TOML mapping for mod names that do not auto-match",
    )
    args = parser.parse_args(argv)

    root = ROOT
    versions = available_versions(root) if args.all or not args.version else [args.version]
    if not versions:
        print("No matching Packwiz/Curseforge version folders found.", file=sys.stderr)
        return 1
    if not args.all and not args.version:
        print(f"No version given; using latest: {versions[-1]}")
        versions = [versions[-1]]

    aliases = load_aliases(args.aliases)
    failed = False
    for version in versions:
        try:
            log = sync_version(
                root,
                version,
                packwiz=args.packwiz,
                update=not args.no_update,
                refresh=not args.no_refresh,
                dry_run=args.dry_run,
                keep_extra=args.keep_extra,
                aliases=aliases,
            )
            print("\n".join(log))
            print()
        except Exception as exc:  # noqa: BLE001 - show the useful local error
            failed = True
            print(f"ERROR {version}: {exc}", file=sys.stderr)

    if args.dry_run:
        print("Dry run: no files were changed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())


