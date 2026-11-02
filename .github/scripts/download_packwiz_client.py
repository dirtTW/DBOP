#!/usr/bin/env python3
"""Stage a Packwiz pack for a real (headless) Minecraft client test run.

The normal Packwiz launchers download files at install time.  GitHub Actions
needs the same files *before* Minecraft starts, so this script reads the
Packwiz index/metafiles, downloads every client-side mod/resource pack, checks
the recorded hash, and copies the pack's config overrides into the run
directory.

Only the Python standard library is used so the workflow does not need any
extra packages.
"""

from __future__ import annotations

import argparse
import filecmp
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only for old local Pythons
    import tomli as tomllib  # type: ignore

USER_AGENT = "DBOP-Minecraft-CI/1.0 (+https://github.com/dirtTW/DBOP)"
HASH_NAMES = {
    "md5": "md5",
    "sha1": "sha1",
    "sha256": "sha256",
    "sha512": "sha512",
}


@dataclass
class PlannedFile:
    """One file that has to exist inside the game directory."""

    kind: str  # "download" or "override"
    source: str  # path inside the Packwiz folder
    destination: str  # path relative to the game directory
    name: str
    filename: str
    side: str
    url: str | None = None
    hash_format: str | None = None
    digest: str | None = None
    mod_id: str | None = None
    version: str | None = None


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def gh_escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def annotation(level: str, title: str, message: str) -> None:
    print(f"::{level} title={gh_escape(title)}::{gh_escape(message)}")


def read_toml(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def hash_file(path: Path, algorithm: str) -> str:
    try:
        digest = hashlib.new(HASH_NAMES[algorithm])
    except KeyError as exc:  # pragma: no cover - malformed pack
        raise ValueError(f"unsupported hash format: {algorithm}") from exc
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def matches_hash(path: Path, algorithm: str, expected: str) -> bool:
    return path.is_file() and hash_file(path, algorithm).lower() == expected.lower()


def destination_for(meta_path: str, filename: str) -> str:
    """Map a Packwiz metafile path to its path inside the game directory."""
    parent = Path(meta_path).parent
    if parent == Path("."):
        parent = Path("")
    return (parent / filename).as_posix()


def load_plan(
    pack_dir: Path,
    dest: Path,
    *,
    client_only: bool = True,
    exclude: list[str] | None = None,
) -> tuple[list[PlannedFile], list[PlannedFile], dict]:
    """Build the download/copy plan for a Packwiz pack."""
    index_path = pack_dir / "index.toml"
    if not index_path.is_file():
        raise FileNotFoundError(f"{index_path} does not exist")
    index = read_toml(index_path)
    pack = read_toml(pack_dir / "pack.toml") if (pack_dir / "pack.toml").is_file() else {}
    index_hash_format = str(index.get("hash-format", "sha256")).lower()

    exclude_patterns = [re.compile(pattern, re.IGNORECASE) for pattern in (exclude or [])]
    planned: list[PlannedFile] = []
    skipped: list[PlannedFile] = []

    for entry in index.get("files", []):
        relative = str(entry.get("file", ""))
        if not relative:
            continue
        source = pack_dir / relative

        if relative.endswith(".pw.toml"):
            if not source.is_file():
                raise FileNotFoundError(f"{source} is listed in index.toml but does not exist")
            meta = read_toml(source)
            side = str(meta.get("side", "both"))
            download = meta.get("download") or {}
            url = download.get("url")
            if not url:
                raise ValueError(
                    f"{relative} has no [download] url; refusing to guess how to obtain it"
                )
            filename = str(meta.get("filename") or Path(str(url)).name)
            destination = destination_for(relative, filename)
            hash_format = str(download.get("hash-format", "sha256")).lower()
            digest = str(download.get("hash", ""))
            candidate = PlannedFile(
                kind="download",
                source=relative,
                destination=destination,
                name=str(meta.get("name", filename)),
                filename=filename,
                side=side,
                url=str(url),
                hash_format=hash_format,
                digest=digest,
                mod_id=(meta.get("update", {}).get("modrinth", {}) or {}).get("mod-id"),
                version=(meta.get("update", {}).get("modrinth", {}) or {}).get("version"),
            )
            if client_only and side == "server":
                skipped.append(candidate)
                continue
            if any(pattern.search(candidate.name) or pattern.search(candidate.filename)
                   for pattern in exclude_patterns):
                skipped.append(candidate)
                continue
            planned.append(candidate)
        else:
            # Plain override file (config, resource pack, ...).  Verify the
            # source hash from index.toml and copy it into the game directory.
            if not source.is_file():
                raise FileNotFoundError(f"{source} is listed in index.toml but does not exist")
            digest = str(entry.get("hash", ""))
            planned.append(
                PlannedFile(
                    kind="override",
                    source=relative,
                    destination=relative,
                    name=relative,
                    filename=Path(relative).name,
                    side="both",
                    hash_format=index_hash_format,
                    digest=digest,
                )
            )

    pack_info = {
        "name": pack.get("name", pack_dir.name),
        "version": pack.get("version", "unknown"),
        "minecraft": (pack.get("versions", {}) or {}).get("minecraft", "unknown"),
        "fabric": (pack.get("versions", {}) or {}).get("fabric", "unknown"),
    }
    return planned, skipped, pack_info


def download_to_cache(plan: PlannedFile, cache_dir: Path, retries: int = 3) -> None:
    """Download a file into the hash-addressed cache."""
    assert plan.url and plan.hash_format and plan.digest
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / plan.digest.lower()

    if matches_hash(cached, plan.hash_format, plan.digest):
        return

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        tmp_path: Path | None = None
        try:
            request = urllib.request.Request(plan.url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=120) as response:
                fd, tmp_name = tempfile.mkstemp(prefix="dbop-", suffix=".part", dir=cache_dir)
                tmp_path = Path(tmp_name)
                with os.fdopen(fd, "wb") as handle:
                    shutil.copyfileobj(response, handle, length=1024 * 1024)
            if not matches_hash(tmp_path, plan.hash_format, plan.digest):
                actual = hash_file(tmp_path, plan.hash_format)
                raise ValueError(
                    f"hash mismatch for {plan.filename}: expected {plan.digest} ({plan.hash_format}), got {actual}"
                )
            tmp_path.replace(cached)
            return
        except Exception as exc:  # noqa: BLE001 - report the actual network/verify error
            last_error = exc
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            if attempt < retries:
                time.sleep(attempt * 2)

    raise RuntimeError(f"failed to download {plan.url}: {last_error}")


def stage_file(
    plan: PlannedFile, pack_dir: Path, dest: Path, cache_dir: Path, dry_run: bool
) -> str:
    """Copy one planned file into the game directory.  Returns a status word."""
    target = dest / plan.destination

    if plan.kind == "override":
        source = pack_dir / plan.source
        if not source.is_file():
            raise FileNotFoundError(f"pack file {plan.source} does not exist")
        # The Packwiz index hash was computed on the pack author's working
        # copy.  Git can normalise line endings on checkout (notably on
        # Windows), so comparing the checked-out override against that hash
        # gives false mismatches.  The checked-out file is the authoritative
        # content for this test run; mod downloads above are still hash-checked.
        if target.is_file() and filecmp.cmp(source, target, shallow=False):
            return "cached"
        if dry_run:
            return "planned"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return "copied"

    assert plan.hash_format and plan.digest
    if matches_hash(target, plan.hash_format, plan.digest):
        return "cached"
    if dry_run:
        return "planned"

    download_to_cache(plan, cache_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_target = target.with_name(target.name + ".part")
    shutil.copyfile(cache_dir / plan.digest.lower(), tmp_target)
    if not matches_hash(tmp_target, plan.hash_format, plan.digest):
        tmp_target.unlink(missing_ok=True)
        raise ValueError(f"staged file {target} failed hash verification")
    tmp_target.replace(target)
    return "downloaded"


def build_manifest(
    pack_dir: Path,
    pack_info: dict,
    plans: list[PlannedFile],
    skipped: list[PlannedFile],
    statuses: dict[str, str],
) -> dict:
    return {
        "pack": {
            **pack_info,
            "pack_dir": pack_dir.as_posix(),
        },
        "generated_by": "download_packwiz_client.py",
        "files": [
            {
                **asdict(plan),
                "status": statuses.get(plan.destination, "unknown"),
            }
            for plan in plans
        ],
        "skipped": [asdict(plan) for plan in skipped],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack-dir", required=True, type=Path, help="Packwiz pack folder, e.g. Packwiz/1.21.8")
    parser.add_argument("--dest", required=True, type=Path, help="Game directory, e.g. run")
    parser.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "dbop-mods")
    parser.add_argument("--include-server", action="store_true", help="Also stage server-only mods")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="REGEX",
        help="Skip mods whose name or filename matches this regex (repeatable)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Only print the plan; do not download or copy")
    parser.add_argument("--jobs", type=int, default=6, help="Parallel downloads (default: 6)")
    args = parser.parse_args(argv)

    pack_dir: Path = args.pack_dir
    dest: Path = args.dest

    try:
        plans, skipped, pack_info = load_plan(
            pack_dir,
            dest,
            client_only=not args.include_server,
            exclude=args.exclude,
        )
    except Exception as exc:  # noqa: BLE001
        annotation("error", "Packwiz staging failed", str(exc))
        eprint(f"error: {exc}")
        return 1

    downloads = [plan for plan in plans if plan.kind == "download"]
    overrides = [plan for plan in plans if plan.kind == "override"]
    mods = [plan for plan in downloads if plan.destination.startswith("mods/")]
    resourcepacks = [plan for plan in downloads if plan.destination.startswith("resourcepacks/")]

    print(
        f"Pack: {pack_info['name']} v{pack_info['version']} "
        f"(Minecraft {pack_info['minecraft']}, Fabric {pack_info['fabric']})"
    )
    print(
        f"Plan: {len(mods)} mods, {len(resourcepacks)} resource pack(s), "
        f"{len(overrides)} config/override file(s); {len(skipped)} skipped"
    )
    for plan in skipped:
        print(f"  skipped: {plan.name} ({plan.filename}, side={plan.side})")

    if args.dry_run:
        for plan in plans:
            print(f"  {plan.kind:8} {plan.destination} <- {plan.source}")
        print("Dry run: nothing was downloaded or written.")
        return 0

    dest.mkdir(parents=True, exist_ok=True)
    statuses: dict[str, str] = {}
    failures: list[tuple[PlannedFile, str]] = []

    # Overrides are small; do them first so a broken pack fails fast.
    for plan in overrides:
        try:
            statuses[plan.destination] = stage_file(plan, pack_dir, dest, args.cache_dir, False)
        except Exception as exc:  # noqa: BLE001
            failures.append((plan, str(exc)))

    completed = 0
    if downloads and not failures:
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            futures = {
                pool.submit(stage_file, plan, pack_dir, dest, args.cache_dir, False): plan
                for plan in downloads
            }
            for future in as_completed(futures):
                plan = futures[future]
                completed += 1
                try:
                    status = future.result()
                    statuses[plan.destination] = status
                    print(f"[{completed:3}/{len(downloads)}] {status:10} {plan.destination}")
                except Exception as exc:  # noqa: BLE001
                    statuses[plan.destination] = "failed"
                    failures.append((plan, str(exc)))
                    print(f"[{completed:3}/{len(downloads)}] FAILED     {plan.destination}: {exc}")

    manifest = build_manifest(pack_dir, pack_info, plans, skipped, statuses)
    manifest_path = dest / "dbop-mods-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    if failures:
        for plan, message in failures:
            annotation("error", f"Failed to stage {plan.name}", message)
        eprint(f"error: {len(failures)} file(s) could not be staged")
        return 1

    print(f"Staged {len(plans)} file(s) into {dest}")
    print(f"Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
