#!/usr/bin/env python3
"""Analyse a headless Minecraft run and try to name the mod that broke it.

The script reads the game log, any crash reports and the installed mod jars.
It maps stack-trace classes / mixin configs / mod ids back to the Fabric mod
that owns them, writes a Markdown report for the GitHub step summary, and emits
GitHub annotations such as::

    ::error title=Suspected crashing mod: Chat Heads (chat_heads)::...

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Iterable

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only for old local Pythons
    import tomli as tomllib  # type: ignore


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Packages that belong to the game, the JVM, the loader or generally shipped
# libraries.  Frames in these packages are evidence, but not directly a mod.
VANILLA_OR_INFRA_PREFIXES = (
    "net.minecraft.",
    "com.mojang.",
    "java.",
    "javax.",
    "jdk.",
    "sun.",
    "com.sun.",
    "org.lwjgl.",
    "io.netty.",
    "it.unimi.",
    "org.slf4j.",
    "org.apache.",
    "com.google.",
    "org.jetbrains.",
    "org.objectweb.asm.",
    "org.spongepowered.asm.",
    "net.fabricmc.loader.",
    "net.fabricmc.api.",
    "net.fabricmc.mapping.",
    "net.fabricmc.accesswidener.",
)

# Ordered: the first match decides the severity.
FATAL_LOG_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\bIncompatible mods found\b", re.I), "fatal", "Fabric Loader rejected the mod set"),
    (re.compile(r"\bMod resolution failed\b", re.I), "fatal", "Fabric Loader could not resolve mods"),
    (re.compile(r"\bUnmet dependency\b", re.I), "fatal", "Fabric Loader reported an unmet dependency"),
    (re.compile(r"\bMixin apply failed\b", re.I), "fatal", "A mixin could not be applied"),
    (re.compile(r"\bMixin transformation of .* failed\b", re.I), "fatal", "A mixin transformation failed"),
    (re.compile(r"\bMixinTransformerError\b", re.I), "fatal", "The Mixin transformer threw an error"),
    (re.compile(r"\bCould not execute entrypoint\b", re.I), "fatal", "A mod entrypoint threw an error"),
    (re.compile(r"\bEntrypoint .* (?:threw|failed)\b", re.I), "fatal", "A mod entrypoint threw an error"),
    (re.compile(r"\bFailed to (?:load|initialize) mod\b", re.I), "fatal", "A mod failed to load"),
    (re.compile(r"\bThe game failed to start\b", re.I), "fatal", "Minecraft aborted during startup"),
    (re.compile(r"currently active LWJGL version is not compatible", re.I), "fatal",
     "Sodium's LWJGL version check aborted startup"),
    (re.compile(r"\bThe game crashed\b", re.I), "fatal", "Minecraft reported a crash"),
    (re.compile(r"\bA fatal error has been detected\b", re.I), "fatal", "The JVM reported a fatal error"),
    (re.compile(r"\bException in thread \"(?:Render thread|main|Server thread|Worker-.*|Netty Client IO.*)\"", re.I),
     "fatal", "An uncaught exception terminated a game thread"),
    (re.compile(r"\bjava\.lang\.(?:NoSuchMethodError|NoClassDefFoundError|ClassNotFoundException|"
                r"IncompatibleClassChangeError|VerifyError|ExceptionInInitializerError)\b"), "error",
     "A class/linkage error occurred"),
    (re.compile(r"\borg\.spongepowered\.asm\.mixin\.transformer\.throwables\.", re.I), "fatal",
     "Mixin threw a transformer exception"),
]

# Lines that are useful context after an error, but not fatal by themselves.
CONTEXT_PATTERN = re.compile(
    r"(Caused by:|Suppressed:|\bat [\w.$]+\(|Exception|Error|Failed|mod |mixin)",
    re.I,
)

DIRECT_MOD_PATTERNS = [
    re.compile(r"(?:from|in) mod ['\"]?([A-Za-z0-9_.-]+)['\"]?", re.I),
    re.compile(r"\bMod ['\"]([^'\"]+)['\"]", re.I),
    re.compile(r"\bmod ['\"]([^'\"]+)['\"]", re.I),
    re.compile(r"\bmod(?:id)?\s+['\"]?([a-z][a-z0-9_.-]{1,63})['\"]?\s+(?:failed|requires|crashed|is missing)", re.I),
    re.compile(r"([A-Za-z0-9_.-]+)\.mixins?\.json", re.I),
    re.compile(r"Mixin apply failed\s+([A-Za-z0-9_.-]+)", re.I),
    re.compile(r"Entrypoint\s+\S+\s+in\s+mod\s+['\"]?([A-Za-z0-9_.-]+)['\"]?", re.I),
]

# Fabric/Knot stack frames look like "at knot//com.example.Foo.bar(...)" or
# "at java.base/java.lang.Thread.run(...)".  Strip the optional class-loader /
# module prefix before capturing the class name.
FRAME_PATTERN = re.compile(
    r"(?:^|\s)at\s+(?:[\w$]+/+)*([\w.$]+)\(", re.M
)
OBSERVED_FRAME_PATTERN = re.compile(
    r"\bat\s+(?:[\w$]+/+)*([\w.$]+)\("
)
OBSERVED_EXCEPTION_PATTERN = re.compile(
    r"([A-Za-z_$][\w.$]*(?:Exception|Error|Throwable))(?::\s*(.*))?$"
)
HS_ERR_FRAME_PATTERN = re.compile(
    r"^\s*[JjVv]\s+\d+\s+(?:c\d+\s+)?([\w.$]+)\(", re.M
)

# Errors that do not contain the mod name in the same line but are known to be
# emitted by a specific mod.  (regex, mod id, weight, evidence)
KNOWN_CAUSE_PATTERNS: list[tuple[re.Pattern[str], str, int, str]] = [
    (
        re.compile(r"currently active LWJGL version is not compatible", re.I),
        "sodium",
        30,
        "Sodium aborted startup because the LWJGL version was empty/incompatible "
        "(a headless launcher patched LWJGL; verify that Xvfb is running)",
    ),
    (
        re.compile(r"link\.caffeinemc\.net/help/sodium", re.I),
        "sodium",
        20,
        "Sodium's help link was printed for the startup failure",
    ),
]
EXCEPTION_PATTERN = re.compile(r"^([\w.$]+(?:Exception|Error|Throwable))(?::\s*(.*))?$")


# ---------------------------------------------------------------------------
# Mod index
# ---------------------------------------------------------------------------


@dataclass
class ModRecord:
    key: str
    mod_id: str
    name: str
    version: str
    jar: str
    nested: str | None = None
    mixin_configs: set[str] = field(default_factory=set)
    classes: set[str] = field(default_factory=set)
    packages: set[str] = field(default_factory=set)
    entrypoints: set[str] = field(default_factory=set)
    pw_toml: str | None = None

    @property
    def display_name(self) -> str:
        return self.name or self.mod_id or self.key


def _as_list(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _entrypoint_value(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("value", "adapter"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    return None


def _mixin_config_names(meta: dict) -> list[str]:
    names: list[str] = []
    for entry in _as_list(meta.get("mixins")):
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict):
            config = entry.get("config")
            if isinstance(config, str):
                names.append(config)
    return names


def _mixin_classes(config: dict) -> Iterable[str]:
    package = str(config.get("package", "")).strip(".")
    for key in ("mixins", "client", "server", "plugin"):
        for entry in _as_list(config.get(key)):
            name: str | None = None
            if isinstance(entry, str):
                name = entry
            elif isinstance(entry, dict):
                name = entry.get("config") or entry.get("name")
            if isinstance(name, str) and name:
                yield f"{package}.{name}" if package else name


class ModIndex:
    """Maps classes, mixin configs and aliases back to Fabric mods."""

    def __init__(self) -> None:
        self.mods: dict[str, ModRecord] = {}
        self.class_to_mods: dict[str, set[str]] = defaultdict(set)
        self.package_to_mods: dict[str, set[str]] = defaultdict(set)
        self.mixin_config_to_mods: dict[str, set[str]] = defaultdict(set)
        self.alias_to_mods: dict[str, set[str]] = defaultdict(set)
        self.jar_to_mods: dict[str, set[str]] = defaultdict(set)
        self.pw_toml_to_mods: dict[str, set[str]] = defaultdict(set)

    def add_jar(self, jar_path: Path, outer: str | None = None, nested: str | None = None) -> None:
        jar_label = outer or jar_path.name
        try:
            with jar_path.open("rb") as handle:
                self._read_jar_bytes(handle.read(), jar_label, jar_path.name, nested)
        except (OSError, zipfile.BadZipFile):
            return

    def _read_jar_bytes(self, data: bytes, jar_label: str, path_label: str, nested: str | None) -> None:
        try:
            archive = zipfile.ZipFile(BytesIO(data))
        except zipfile.BadZipFile:
            return

        with archive:
            meta: dict = {}
            if "fabric.mod.json" in archive.namelist():
                try:
                    meta = json.loads(archive.read("fabric.mod.json").decode("utf-8", "replace"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    meta = {}

            if not meta:
                # Only Fabric mods are indexed.  Nested jars without their own
                # fabric.mod.json are plain libraries.
                return
            mod_id = str(meta.get("id") or jar_label)
            name = str(meta.get("name") or mod_id)
            version = str(meta.get("version") or "")

            key = mod_id.lower()
            record = self.mods.get(key)
            if record is None:
                record = ModRecord(
                    key=key,
                    mod_id=mod_id,
                    name=name,
                    version=version,
                    jar=jar_label,
                    nested=nested,
                )
                self.mods[key] = record
            else:
                if not record.name and name:
                    record.name = name
                if not record.version and version:
                    record.version = version
                if nested and not record.nested:
                    record.nested = nested

            self.jar_to_mods[jar_label].add(key)
            for alias in {mod_id, name, name.replace(" ", ""), Path(path_label).stem}:
                normalized = alias.strip().lower()
                if len(normalized) >= 3:
                    self.alias_to_mods[normalized].add(key)

            for stage, entries in (meta.get("entrypoints") or {}).items():
                for entry in _as_list(entries):
                    cls = _entrypoint_value(entry)
                    if not cls:
                        continue
                    cls = cls.split("::")[0].strip()
                    if not re.fullmatch(r"[\w.$]+", cls):
                        continue
                    record.entrypoints.add(cls)
                    record.classes.add(cls)
                    if "." in cls:
                        record.packages.add(cls.rsplit(".", 1)[0])

            for config_name in _mixin_config_names(meta):
                record.mixin_configs.add(config_name)
                config_path = config_name.lstrip("/")
                if config_path in archive.namelist():
                    try:
                        mixin_config = json.loads(archive.read(config_path).decode("utf-8", "replace"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        mixin_config = {}
                    for cls in _mixin_classes(mixin_config):
                        record.classes.add(cls)
                        if "." in cls:
                            record.packages.add(cls.rsplit(".", 1)[0])
                config_basename = Path(config_name).name
                self.mixin_config_to_mods[config_basename.lower()].add(key)
                stem = Path(config_basename).stem.lower()  # chat_heads.mixins
                self.alias_to_mods[stem].add(key)
                # Also accept the short name used by Mixin error messages:
                # "chat_heads.mixins.json" -> "chat_heads"
                self.alias_to_mods[stem.split(".mixins", 1)[0]].add(key)

            for name in archive.namelist():
                normalized = name
                if normalized.startswith("META-INF/versions/"):
                    parts = normalized.split("/", 3)
                    if len(parts) == 4:
                        normalized = parts[3]
                if normalized.startswith("META-INF/"):
                    continue
                if not normalized.endswith(".class"):
                    continue
                class_name = normalized[:-6].replace("/", ".")
                if class_name.endswith(("module-info", "package-info")):
                    continue
                record.classes.add(class_name)
                if "." in class_name:
                    record.packages.add(class_name.rsplit(".", 1)[0])

            # Fabric API and some other mods bundle nested jars.
            for name in archive.namelist():
                if not name.startswith("META-INF/jars/") or not name.endswith(".jar"):
                    continue
                nested_name = Path(name).name
                try:
                    self._read_jar_bytes(archive.read(name), jar_label, nested_name, nested_name)
                except (OSError, zipfile.BadZipFile):
                    continue

    def finalize(self) -> None:
        for key, record in self.mods.items():
            for cls in record.classes:
                self.class_to_mods[cls].add(key)
            for package in record.packages:
                self.package_to_mods[package].add(key)

    def attach_pack_metadata(self, pack_dir: Path) -> None:
        """Link jars back to Packwiz metafiles for clickable annotations."""
        for meta_path in sorted(pack_dir.glob("mods/*.pw.toml")):
            try:
                with meta_path.open("rb") as handle:
                    meta = tomllib.load(handle)
            except (OSError, tomllib.TOMLDecodeError):
                continue
            filename = str(meta.get("filename") or "")
            if not filename:
                continue
            for key in self.jar_to_mods.get(filename, set()):
                self.mods[key].pw_toml = meta_path.as_posix()
                self.pw_toml_to_mods[meta_path.as_posix()].add(key)

    def resolve_token(self, token: str) -> set[str]:
        normalized = token.strip().strip("'\"").lower()
        if not normalized:
            return set()
        candidates: set[str] = set()
        variants = {normalized, normalized + ".json", normalized + ".mixins.json"}
        variants.discard("")
        for variant in variants:
            if variant in self.mods:
                candidates.add(variant)
            if variant in self.alias_to_mods:
                candidates |= self.alias_to_mods[variant]
            if variant in self.mixin_config_to_mods:
                candidates |= self.mixin_config_to_mods[variant]
        # Jar-like token, e.g. chat_heads-0.14.0-fabric-1.21.6.jar
        if normalized.endswith(".jar"):
            candidates |= self.jar_to_mods.get(token.strip(), set())
        return candidates

    def match_class(self, fqcn: str) -> tuple[set[str], str]:
        """Return (mods, evidence-kind) for a fully qualified class name."""
        class_name = fqcn.split("$", 1)[0]
        if class_name in self.class_to_mods:
            return self.class_to_mods[class_name], "class"
        parts = class_name.split(".")
        for i in range(len(parts) - 1, 1, -1):
            package = ".".join(parts[:i])
            if package in self.package_to_mods:
                return self.package_to_mods[package], "package"
        return set(), ""


def is_infra_frame(fqcn: str) -> bool:
    return any(fqcn.startswith(prefix) for prefix in VANILLA_OR_INFRA_PREFIXES)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    severity: str
    message: str
    line: str
    line_number: int | None = None
    source: str = ""


@dataclass
class ObservedError:
    exception: str
    message: str
    line: str
    line_number: int
    frames: list[str] = field(default_factory=list)


@dataclass
class Analysis:
    status: str
    passed: bool
    reason: str
    crash_report: str | None = None
    description: str = ""
    exception: str = ""
    suspects: list[dict] = field(default_factory=list)
    observed_errors: list[dict] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    started: bool = False
    stopped: bool = False
    log_tail: list[str] = field(default_factory=list)
    log_path: str | None = None
    mod_count: int = 0
    minecraft_version: str = ""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _trim(line: str, limit: int = 300) -> str:
    line = line.replace("\r", "").strip()
    return line if len(line) <= limit else line[: limit - 3] + "..."


def extract_frames(text: str) -> list[str]:
    return [match.group(1) for match in FRAME_PATTERN.finditer(text)]


def extract_hs_err_frames(text: str) -> list[str]:
    return [match.group(1) for match in HS_ERR_FRAME_PATTERN.finditer(text)]


def extract_direct_tokens(text: str) -> list[tuple[str, str]]:
    """Return (token, matching line) pairs for direct mod references."""
    tokens: list[tuple[str, str]] = []
    for pattern in DIRECT_MOD_PATTERNS:
        for match in pattern.finditer(text):
            token = match.group(1).strip()
            if token:
                tokens.append((token, _trim(match.group(0))))
    return tokens


def parse_crash_report(text: str) -> dict:
    lines = text.splitlines()
    description = ""
    exceptions: list[str] = []
    mixin_lines: list[str] = []
    context: list[str] = []

    for index, raw in enumerate(lines):
        line = raw.rstrip()
        stripped = line.strip()
        if not description and stripped.startswith("Description:"):
            description = stripped[len("Description:"):].strip()
        if stripped.startswith("Caused by:"):
            exceptions.append(stripped)
            context.append(stripped)
            # Keep the next few frames for attribution.
            context.extend(_trim(item) for item in lines[index + 1:index + 12])
        elif EXCEPTION_PATTERN.match(stripped):
            exceptions.append(stripped)
            context.append(stripped)
        if "mixin" in stripped.lower() or ".mixins.json" in stripped.lower():
            mixin_lines.append(stripped)
            context.extend(_trim(item) for item in lines[index:index + 8])

    # The first 120 lines contain the description and the main stack trace.
    context = [*map(_trim, lines[:120]), *context]
    return {
        "description": description,
        "exceptions": exceptions,
        "frames": extract_frames(text),
        "mixin_lines": mixin_lines,
        "context": context,
        "tokens": extract_direct_tokens("\n".join(context)),
    }


def parse_latest_log(path: Path) -> tuple[list[Finding], list[str], bool, bool]:
    text = _read_text(path)
    lines = text.splitlines()
    findings: list[Finding] = []
    context: list[str] = []
    started = bool(
        re.search(r"Loading Minecraft|Backend library:|Setting user:|Fabric Loader", text)
    )
    stopped = bool(re.search(r"Stopping!|Stopping server|Game closed|Stopping Minecraft", text, re.I))

    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped:
            continue
        for pattern, severity, message in FATAL_LOG_PATTERNS:
            if pattern.search(stripped):
                # Mods routinely probe for optional classes and log a caught
                # ClassNotFoundException at WARN level.  That is not a failure.
                if (
                    severity == "error"
                    and "/WARN]" in stripped
                    and ("Error loading class:" in stripped or "ClassNotFoundException" in stripped)
                ):
                    severity = "warning"
                findings.append(
                    Finding(
                        severity=severity,
                        message=message,
                        line=_trim(stripped),
                        line_number=index + 1,
                        source=path.name,
                    )
                )
                context.append(_trim(stripped))
                context.extend(_trim(item) for item in lines[index + 1:index + 21] if item.strip())
                break

    # Keep only unique context lines while preserving order.
    seen: set[str] = set()
    unique_context: list[str] = []
    for line in context:
        if line not in seen:
            seen.add(line)
            unique_context.append(line)
    return findings, unique_context, started, stopped


def parse_observed_errors(text: str) -> list[ObservedError]:
    """Find caught exceptions that still deserve a mod-attribution warning.

    These do not stop the game, so they are reported as warnings rather than
    failures.  A typical example is a mod printing a stack trace from a failed
    optional platform integration (for example ``ibus`` on a headless Linux
    runner).
    """
    lines = text.splitlines()
    observed: list[ObservedError] = []
    seen: set[tuple[str, str]] = set()

    for index, raw in enumerate(lines):
        stripped = raw.strip()
        # "Caused by:" is part of an exception already reported above.
        if not stripped or "Caused by:" in stripped:
            continue
        # These are the routine optional-class probes; another mod is merely
        # checking whether a class exists.
        if "Error loading class:" in stripped:
            continue
        match = OBSERVED_EXCEPTION_PATTERN.search(stripped)
        if not match:
            continue
        exception = match.group(1)
        message = (match.group(2) or "").strip()
        if exception in {"Error", "Exception", "Throwable"}:
            continue

        frames: list[str] = []
        for following in lines[index + 1:index + 80]:
            next_line = following.strip()
            frame_match = OBSERVED_FRAME_PATTERN.search(next_line)
            if frame_match:
                frames.append(frame_match.group(1))
                continue
            if next_line.startswith(("...", "Suppressed:", "Caused by:")):
                continue
            if frames:
                break
        if len(frames) < 2:
            continue

        key = (exception, message)
        if key in seen:
            continue
        seen.add(key)
        observed.append(
            ObservedError(
                exception=exception,
                message=message,
                line=_trim(stripped),
                line_number=index + 1,
                frames=frames,
            )
        )
        if len(observed) >= 20:
            break

    return observed


def parse_hmc_log(path: Path) -> list[Finding]:
    if not path.is_file():
        return []
    findings: list[Finding] = []
    lines = _read_text(path).splitlines()
    patterns = [
        (re.compile(r"(?:found|detected|wrote|new) crash report", re.I), "HMC found a crash report"),
        (re.compile(r"crash report (?:found|detected|written)", re.I), "HMC found a crash report"),
        (re.compile(r"Minecraft (?:crashed|exited)", re.I), "HeadlessMC reported that Minecraft exited"),
        (re.compile(r"Process exited with (?:code|value)\s+[1-9]", re.I), "HeadlessMC saw a non-zero game exit code"),
        (re.compile(r"Failed to launch", re.I), "HeadlessMC failed to launch the game"),
    ]
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        for pattern, message in patterns:
            if pattern.search(stripped):
                findings.append(
                    Finding(
                        severity="warning",
                        message=message,
                        line=_trim(stripped),
                        line_number=index + 1,
                        source=path.name,
                    )
                )
                break
    return findings


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


class Attributor:
    def __init__(self, index: ModIndex) -> None:
        self.index = index
        self.scores: dict[str, int] = defaultdict(int)
        self.evidence: dict[str, list[str]] = defaultdict(list)

    def add(self, key: str, weight: int, evidence: str) -> None:
        if key not in self.index.mods:
            return
        self.scores[key] += weight
        if evidence not in self.evidence[key]:
            self.evidence[key].append(evidence)

    def add_tokens(self, tokens: Iterable[tuple[str, str]], base_weight: int = 6) -> None:
        for token, line in tokens:
            keys = self.index.resolve_token(token)
            for key in keys:
                # Jar names and mixin configs are much stronger evidence than a
                # bare id/name mention.
                weight = base_weight
                lowered = token.lower()
                if lowered.endswith(".jar"):
                    weight = max(weight, 10)
                elif ".mixins" in lowered or lowered.endswith(".json"):
                    weight = max(weight, 10)
                self.add(key, weight, f"mentioned in log: {line}")

    def add_frames(self, frames: Iterable[str], per_frame: int = 3, cap: int = 15) -> None:
        seen: dict[str, int] = defaultdict(int)
        for fqcn in frames:
            if is_infra_frame(fqcn):
                continue
            keys, _kind = self.index.match_class(fqcn)
            if not keys:
                continue
            for key in keys:
                if seen[key] >= cap:
                    continue
                seen[key] += per_frame
                self.add(key, per_frame, f"stack frame: {fqcn}")

    def add_mixin_configs(self, text: str) -> None:
        lowered = text.lower()
        for config, keys in self.index.mixin_config_to_mods.items():
            if config in lowered:
                for key in keys:
                    self.add(key, 12, f"mixin config: {config}")

    def suspects(self, limit: int = 5) -> list[dict]:
        ordered = sorted(self.scores.items(), key=lambda item: (-item[1], item[0]))
        result: list[dict] = []
        for key, score in ordered[:limit]:
            if score <= 0:
                continue
            record = self.index.mods[key]
            result.append(
                {
                    "id": record.mod_id,
                    "name": record.display_name,
                    "version": record.version,
                    "jar": record.jar,
                    "nested": record.nested,
                    "pw_toml": record.pw_toml,
                    "score": score,
                    "evidence": self.evidence[key][:8],
                }
            )
        return result


def build_index(run_dir: Path, pack_dir: Path) -> ModIndex:
    index = ModIndex()
    mods_dir = run_dir / "mods"
    if mods_dir.is_dir():
        for jar in sorted(mods_dir.glob("*.jar")):
            index.add_jar(jar)
    index.finalize()
    index.attach_pack_metadata(pack_dir)
    return index


def analyze(
    *,
    run_dir: Path,
    hmc_dir: Path,
    pack_dir: Path,
    game_outcome: str = "success",
    strict: bool = False,
    minecraft_version: str = "",
) -> Analysis:
    index = build_index(run_dir, pack_dir)
    attributor = Attributor(index)

    logs_dir = run_dir / "logs"
    latest_log = logs_dir / "latest.log"
    crash_reports = sorted(
        (run_dir / "crash-reports").glob("crash-*.txt"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    ) if (run_dir / "crash-reports").is_dir() else []
    hs_err_reports = sorted(
        [
            *(run_dir.glob("hs_err_pid*.log")),
            *((run_dir / "crash-reports").glob("hs_err_pid*.log")
              if (run_dir / "crash-reports").is_dir() else []),
        ],
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )

    findings: list[Finding] = []
    log_context: list[str] = []
    started = False
    stopped = False
    log_tail: list[str] = []
    description = ""
    exception = ""

    if latest_log.is_file():
        log_findings, log_context, started, stopped = parse_latest_log(latest_log)
        findings.extend(log_findings)
        log_tail = [_trim(line) for line in _read_text(latest_log).splitlines()[-50:]]
        attributor.add_tokens(extract_direct_tokens("\n".join(log_context)), base_weight=6)
        attributor.add_frames(extract_frames("\n".join(log_context)))
        attributor.add_mixin_configs("\n".join(log_context))

        for finding in log_findings:
            if finding.severity == "fatal":
                attributor.add_tokens(extract_direct_tokens(finding.line), base_weight=8)

    crash_text = ""
    crash_path: str | None = None
    hs_err_text = ""
    hs_err_path: str | None = None
    if crash_reports:
        crash_path = crash_reports[0].as_posix()
        crash_text = _read_text(crash_reports[0])
        crash = parse_crash_report(crash_text)
        description = crash["description"]
        if crash["exceptions"]:
            exception = crash["exceptions"][0]
        attributor.add_tokens(crash["tokens"], base_weight=6)
        attributor.add_frames(crash["frames"])
        attributor.add_mixin_configs(crash_text)
        findings.append(
            Finding(
                severity="fatal",
                message="Minecraft wrote a crash report",
                line=_trim(description or crash_path),
                source=crash_reports[0].name,
            )
        )

    if hs_err_reports:
        hs_err_path = hs_err_reports[0].as_posix()
        hs_err_text = _read_text(hs_err_reports[0])
        attributor.add_frames(extract_hs_err_frames(hs_err_text))
        attributor.add_frames(extract_frames(hs_err_text))
        attributor.add_mixin_configs(hs_err_text)
        findings.append(
            Finding(
                severity="fatal",
                message="The JVM wrote an hs_err_pid crash log",
                line=_trim(hs_err_path),
                source=hs_err_reports[0].name,
            )
        )

    observed_errors: list[dict] = []
    if latest_log.is_file() and not crash_reports and not hs_err_reports:
        fatal_lines = {
            finding.line_number
            for finding in findings
            if finding.severity in {"fatal", "error"} and finding.line_number
        }
        for block in parse_observed_errors(_read_text(latest_log)):
            if block.line_number in fatal_lines:
                continue
            observed_attributor = Attributor(index)
            observed_attributor.add_frames(block.frames)
            observed_attributor.add_tokens(
                extract_direct_tokens(block.line), base_weight=6
            )
            observed_suspects = observed_attributor.suspects(limit=3)
            if not observed_suspects:
                continue
            observed_errors.append(
                {
                    "exception": block.exception,
                    "message": block.message,
                    "line": block.line,
                    "line_number": block.line_number,
                    "suspects": observed_suspects,
                }
            )
            findings.append(
                Finding(
                    severity="warning",
                    message=f"non-fatal exception: {block.exception}",
                    line=block.line,
                    line_number=block.line_number,
                    source="latest.log",
                )
            )
            if len(observed_errors) >= 10:
                break

    # Some mods print an error that only identifies them through a distinctive
    # message or help link, without a stack frame that points at their classes.
    known_context = "\n".join(log_context) + "\n" + crash_text + "\n" + hs_err_text
    for pattern, mod_id, weight, evidence in KNOWN_CAUSE_PATTERNS:
        if pattern.search(known_context):
            attributor.add(mod_id.lower(), weight, evidence)

    findings.extend(parse_hmc_log(hmc_dir / "logs" / "headlessmc.log"))

    suspects = attributor.suspects()

    # Decide the overall result.
    if crash_reports or hs_err_reports:
        status, passed = "crash", False
        if crash_reports:
            reason = f"Minecraft crashed: {description or 'see crash report'}"
        else:
            reason = "the JVM wrote an hs_err_pid crash log"
            crash_path = hs_err_path
            description = description or "JVM fatal error"
    elif not latest_log.is_file():
        status, passed = "did_not_start", False
        reason = "run/logs/latest.log was not created; Minecraft did not start"
    elif not started:
        status, passed = "did_not_start", False
        reason = "latest.log exists but contains no Minecraft startup marker"
    elif any(finding.severity == "fatal" for finding in findings):
        status, passed = "error", False
        fatal = next(finding for finding in findings if finding.severity == "fatal")
        reason = f"fatal error in the game log: {fatal.message}"
    elif game_outcome == "failure":
        status, passed = "game_failed", False
        reason = "the Minecraft/HeadlessMC step failed (non-zero exit)"
    elif strict and any(finding.severity == "error" for finding in findings):
        status, passed = "error", False
        error = next(finding for finding in findings if finding.severity == "error")
        reason = f"error in the game log: {error.message}"
    else:
        status, passed = "pass", True
        reason = "the client started and no crash report or fatal log entry was found"

    if passed and not stopped:
        reason += " (warning: no clean shutdown marker was found)"
    if passed and observed_errors:
        reason += f" ({len(observed_errors)} non-fatal mod error(s) observed)"

    return Analysis(
        status=status,
        passed=passed,
        reason=reason,
        crash_report=crash_path,
        description=description,
        exception=exception,
        suspects=suspects,
        observed_errors=observed_errors,
        findings=findings,
        started=started,
        stopped=stopped,
        log_tail=log_tail,
        log_path=latest_log.as_posix() if latest_log.exists() else None,
        # Count installed jars, not Fabric API's nested modules.
        mod_count=len(list((run_dir / "mods").glob("*.jar"))) if (run_dir / "mods").is_dir() else 0,
        minecraft_version=minecraft_version,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def gh_escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def annotate(level: str, title: str, message: str, file: str | None = None) -> None:
    location = f" file={gh_escape(file)},line=1" if file else ""
    print(f"::{level}{location},title={gh_escape(title)}::{gh_escape(message)}")


def suspect_summary(suspect: dict) -> str:
    evidence = "; ".join(suspect.get("evidence", [])[:3])
    return f"{suspect['name']} ({suspect['id']}): {evidence}"


def emit_annotations(result: Analysis) -> None:
    if result.passed:
        annotate(
            "notice",
            "Minecraft Fabric client test passed",
            f"{result.reason}. Analysed {result.mod_count} mods.",
        )
    elif result.suspects:
        for suspect in result.suspects:
            annotate(
                "error",
                f"Suspected mod: {suspect['name']} ({suspect['id']})",
                suspect_summary(suspect),
                suspect.get("pw_toml"),
            )
    else:
        annotate(
            "error",
            "Minecraft Fabric client test failed",
            f"{result.reason} No mod-specific culprit could be identified; see the uploaded logs.",
            result.crash_report,
        )

    for observed in result.observed_errors:
        top = observed["suspects"][0]
        annotate(
            "warning",
            f"Mod logged an error: {top['name']} ({top['id']})",
            f"{observed['exception']}: {observed['message']}",
            top.get("pw_toml"),
        )


def render_markdown(result: Analysis) -> str:
    icon = "✅" if result.passed else "❌"
    lines = [
        "## Minecraft Fabric client test",
        "",
        f"**Result: {icon} {'PASSED' if result.passed else 'FAILED'}** — {result.reason}",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Status | `{result.status}` |",
        f"| Minecraft | `{result.minecraft_version or 'unknown'}` |",
        f"| Mods analysed | {result.mod_count} |",
        f"| Client started | {'yes' if result.started else 'no'} |",
        f"| Clean shutdown | {'yes' if result.stopped else 'no'} |",
        f"| Crash report | `{result.crash_report or 'none'}` |",
    ]
    if result.description:
        lines.append(f"| Crash description | {result.description} |")
    if result.exception:
        lines.append(f"| Exception | `{result.exception}` |")
    lines.append("")

    if result.suspects:
        lines += [
            "### Suspected mod(s)",
            "",
            "| Mod | ID | Version | Jar | Score | Evidence |",
            "| --- | --- | --- | --- | ---: | --- |",
        ]
        for suspect in result.suspects:
            evidence = "<br>".join(
                item.replace("|", "\\|") for item in suspect.get("evidence", [])[:5]
            )
            lines.append(
                f"| **{suspect['name']}** | `{suspect['id']}` | {suspect.get('version') or '?'} | "
                f"`{suspect['jar']}` | {suspect['score']} | {evidence} |"
            )
        lines.append("")
    elif not result.passed:
        lines += [
            "### Suspected mod(s)",
            "",
            "No mod-specific culprit could be identified from the stack trace, mixin configs or "
            "loader messages. The crash may be environmental (graphics driver, memory, JVM) or the "
            "failing mod may not appear in the trace.",
            "",
        ]

    if result.observed_errors:
        lines += [
            "### Observed errors (non-fatal)",
            "",
            "These exceptions did not stop the client, but the stack trace points at a mod. "
            "They are reported as warnings, not failures.",
            "",
            "| Mod | ID | Exception | Evidence |",
            "| --- | --- | --- | --- |",
        ]
        for observed in result.observed_errors:
            top = observed["suspects"][0]
            evidence = "<br>".join(
                item.replace("|", "\\|") for item in top.get("evidence", [])[:3]
            )
            exception = f"{observed['exception']}: {observed['message']}".replace("|", "\\|")
            lines.append(
                f"| **{top['name']}** | `{top['id']}` | {exception} | {evidence} |"
            )
        lines.append("")

    if result.findings:
        lines += ["### Detected errors", ""]
        for finding in result.findings[:20]:
            location = f" (line {finding.line_number})" if finding.line_number else ""
            lines.append(
                f"- **{finding.severity.upper()}** {finding.message}{location}: `{finding.line}`"
            )
        lines.append("")

    if result.log_tail:
        lines += [
            "<details><summary>Tail of the Minecraft log</summary>",
            "",
            "```text",
            *result.log_tail[-40:],
            "```",
            "",
            "</details>",
            "",
        ]

    lines += [
        "### Artifacts",
        "",
        "The workflow uploads `run/logs`, `run/crash-reports` and the HeadlessMC log. "
        "Download the **minecraft-client-test** artifact to inspect the full run.",
        "",
    ]
    return "\n".join(lines)


def write_github_output(path: Path, result: Analysis) -> None:
    suspect_ids = ",".join(suspect["id"] for suspect in result.suspects)
    values = {
        "status": result.status,
        "passed": "true" if result.passed else "false",
        "suspects": suspect_ids,
        "reason": result.reason.replace("\n", " "),
    }
    with path.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def print_report(result: Analysis) -> None:
    print(f"Minecraft Fabric client test: {result.status.upper()}")
    print(f"Reason: {result.reason}")
    if result.crash_report:
        print(f"Crash report: {result.crash_report}")
    if result.exception:
        print(f"Exception: {result.exception}")
    if result.suspects:
        print("Suspected mod(s):")
        for suspect in result.suspects:
            print(
                f"  - {suspect['name']} ({suspect['id']}) score={suspect['score']} "
                f"jar={suspect['jar']}"
            )
            for evidence in suspect.get("evidence", [])[:5]:
                print(f"      {evidence}")
    else:
        print("Suspected mod(s): none identified")
    if result.observed_errors:
        print("Observed non-fatal mod errors:")
        for observed in result.observed_errors:
            top = observed["suspects"][0]
            print(
                f"  - {top['name']} ({top['id']}): "
                f"{observed['exception']}: {observed['message']}"
            )
    if result.findings:
        print("Findings:")
        for finding in result.findings[:10]:
            print(f"  - [{finding.severity}] {finding.message}: {finding.line}")


def resolve_minecraft_version(pack_dir: Path) -> str:
    pack_toml = pack_dir / "pack.toml"
    if not pack_toml.is_file():
        return ""
    try:
        with pack_toml.open("rb") as handle:
            pack = tomllib.load(handle)
        return str((pack.get("versions") or {}).get("minecraft", ""))
    except (OSError, tomllib.TOMLDecodeError):
        return ""


def main(argv: list[str] | None = None) -> int:
    # Windows terminals often default to cp1252; the report contains emoji.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=Path("run"), help="Minecraft game directory")
    parser.add_argument("--hmc-dir", type=Path, default=Path("HeadlessMC"), help="HeadlessMC data directory")
    parser.add_argument("--pack-dir", type=Path, default=Path("Packwiz/1.21.8"), help="Packwiz pack folder")
    parser.add_argument("--game-outcome", default="success", help="Outcome of the Minecraft step (success/failure)")
    parser.add_argument("--strict", action="store_true", help="Also fail on serious (but non-crash) log errors")
    parser.add_argument("--json", type=Path, help="Write the machine-readable result here")
    parser.add_argument("--summary", type=Path, help="Append the Markdown report here (e.g. $GITHUB_STEP_SUMMARY)")
    parser.add_argument("--github-output", type=Path, help="Append status/passed/suspects to this file")
    parser.add_argument("--minecraft-version", default="", help="Minecraft version for the report")
    args = parser.parse_args(argv)

    minecraft_version = args.minecraft_version or resolve_minecraft_version(args.pack_dir)

    try:
        result = analyze(
            run_dir=args.run_dir,
            hmc_dir=args.hmc_dir,
            pack_dir=args.pack_dir,
            game_outcome=args.game_outcome,
            strict=args.strict,
            minecraft_version=minecraft_version,
        )
    except Exception as exc:  # noqa: BLE001 - never hide a harness error
        traceback.print_exc()
        annotate("error", "Minecraft log analysis failed", str(exc))
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as handle:
                handle.write("status=analysis_error\npassed=false\nsuspects=\nreason=analysis failed\n")
        return 2

    print_report(result)
    emit_annotations(result)
    markdown = render_markdown(result)

    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        with args.summary.open("a", encoding="utf-8") as handle:
            handle.write(markdown)
            handle.write("\n")
    else:
        print("\n" + markdown)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": result.status,
            "passed": result.passed,
            "reason": result.reason,
            "minecraft_version": result.minecraft_version,
            "crash_report": result.crash_report,
            "description": result.description,
            "exception": result.exception,
            "started": result.started,
            "stopped": result.stopped,
            "mod_count": result.mod_count,
            "suspects": result.suspects,
            "observed_errors": result.observed_errors,
            "findings": [
                {
                    "severity": finding.severity,
                    "message": finding.message,
                    "line": finding.line,
                    "line_number": finding.line_number,
                    "source": finding.source,
                }
                for finding in result.findings
            ],
            "log_tail": result.log_tail,
            "log_path": result.log_path,
        }
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    if args.github_output:
        write_github_output(args.github_output, result)

    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
