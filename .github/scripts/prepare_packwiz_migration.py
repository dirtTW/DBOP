#!/usr/bin/env python3

import argparse
import json
import os
import re
import shutil
import urllib.request
from pathlib import Path


VERSION_MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
FABRIC_LOADER_URL = "https://meta.fabricmc.net/v2/versions/loader/{version}"
VERSION_PATTERN = re.compile(r"\d+(?:\.\d+){1,2}\Z")
SEMVER_PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)\Z")


def fetch_json(url: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": "DBOP-packwiz-migration"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def set_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")


def replace_toml_value(contents: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
    updated, count = pattern.subn(f'{key} = "{value}"', contents)
    if count != 1:
        raise ValueError(f"Expected exactly one {key!r} field in pack.toml; found {count}")
    return updated


def increment_patch_version(version: str) -> str:
    match = SEMVER_PATTERN.fullmatch(version)
    if not match:
        raise ValueError(f"Expected a three-part pack version, found {version!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return f"{major}.{minor}.{patch + 1}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--packwiz-root",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "Packwiz",
    )
    args = parser.parse_args()
    pack_root = args.packwiz_root.resolve()
    manifest = fetch_json(VERSION_MANIFEST_URL)
    version_records = manifest["versions"]
    versions_by_id = {version["id"]: version for version in version_records}

    requested_version = os.environ.get("MINECRAFT_VERSION", "").strip()
    if requested_version:
        if not VERSION_PATTERN.fullmatch(requested_version):
            raise ValueError(f"Invalid Minecraft version: {requested_version!r}")
        version_record = versions_by_id.get(requested_version)
        if not version_record or version_record["type"] != "release":
            raise ValueError(f"{requested_version} is not a stable Minecraft release in Mojang's manifest")
        target_version = requested_version
    else:
        target_version = manifest["latest"]["release"]

    target_dir = pack_root / target_version
    if target_dir.exists():
        print(f"Packwiz/{target_version} already exists; no migration is needed.")
        set_output("skip", "true")
        set_output("target_version", target_version)
        return

    source_candidates = [
        path
        for path in pack_root.iterdir()
        if path.is_dir()
        and (path / "pack.toml").is_file()
        and path.name in versions_by_id
        and versions_by_id[path.name]["type"] == "release"
    ]
    if not source_candidates:
        raise FileNotFoundError("No existing stable-version Packwiz folder was found")
    source_dir = max(
        source_candidates,
        key=lambda path: versions_by_id[path.name]["releaseTime"],
    )
    source_version = source_dir.name

    loaders = fetch_json(FABRIC_LOADER_URL.format(version=target_version))
    stable_loaders = [entry["loader"]["version"] for entry in loaders if entry["loader"]["stable"]]
    if not stable_loaders:
        raise RuntimeError(f"No stable Fabric Loader build is available for Minecraft {target_version}")
    loader_version = stable_loaders[0]

    source_pack = (source_dir / "pack.toml").read_text(encoding="utf-8")
    source_name_match = re.search(r'^name\s*=\s*"([^"]+)"\s*$', source_pack, re.MULTILINE)
    if not source_name_match:
        raise ValueError(f"Could not read pack name from {source_dir / 'pack.toml'}")
    source_version_match = re.search(r'^version\s*=\s*"([^"]+)"\s*$', source_pack, re.MULTILINE)
    if not source_version_match:
        raise ValueError(f"Could not read pack version from {source_dir / 'pack.toml'}")
    source_pack_version = source_version_match.group(1)
    target_pack_version = increment_patch_version(source_pack_version)
    target_name = f"DBOP MC {target_version}"

    shutil.copytree(source_dir, target_dir)
    target_pack_path = target_dir / "pack.toml"
    target_pack = target_pack_path.read_text(encoding="utf-8")
    target_pack = replace_toml_value(target_pack, "name", target_name)
    target_pack = replace_toml_value(target_pack, "version", target_pack_version)
    target_pack = replace_toml_value(target_pack, "minecraft", target_version)
    target_pack = replace_toml_value(target_pack, "fabric", loader_version)
    target_pack_path.write_text(target_pack, encoding="utf-8")

    credits_path = target_dir / "config" / "isxander-main-menu-credits.json"
    if credits_path.exists():
        credits = credits_path.read_text(encoding="utf-8")
        credits = credits.replace(
            f"DirtTW's Basement Optimize Pack v{source_pack_version}",
            f"DirtTW's Basement Optimize Pack v{target_pack_version}",
        )
        credits_path.write_text(credits, encoding="utf-8")

    dependencies_path = target_dir / "config" / "fabric_loader_dependencies.json"
    if not dependencies_path.exists():
        raise FileNotFoundError(f"Missing Fabric dependency overrides: {dependencies_path}")
    dependencies = json.loads(dependencies_path.read_text(encoding="utf-8"))
    overrides = dependencies.setdefault("overrides", {})
    fabric_loader = overrides.setdefault("fabricloader", {}).setdefault("+depends", {})
    fabric_loader["fabricloader"] = f">={loader_version}"

    recommendations = overrides.setdefault("minecraft", {}).setdefault("+recommends", {})
    old_name = source_name_match.group(1)
    old_version_constraint = recommendations.pop(old_name, None)
    if old_version_constraint is not None:
        recommendations[target_name] = old_version_constraint
    dependencies_path.write_text(
        json.dumps(dependencies, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"Prepared Packwiz/{target_version} from Packwiz/{source_version} with Fabric Loader {loader_version}.")
    set_output("skip", "false")
    set_output("target_version", target_version)
    set_output("source_version", source_version)
    set_output("source_pack_version", source_pack_version)
    set_output("target_pack_version", target_pack_version)
    set_output("fabric_loader", loader_version)


if __name__ == "__main__":
    main()
