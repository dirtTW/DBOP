# Minecraft client test harness

`../workflows/minecraft-client-test.yml` launches a **real Minecraft Fabric
client** in CI, loads the client-side files from a Packwiz pack, and reports the
mod responsible if the game crashes or logs a fatal error.

## What the workflow does

1. Reads the Minecraft/Fabric versions from `Packwiz/<version>/pack.toml`.
2. Downloads every client-side mod/resource pack listed by `index.toml` with
   `download_packwiz_client.py`. Every file is checked against the Packwiz
   hash before it is used.
3. Installs that exact Minecraft version and Fabric loader version with
   HeadlessMC.
4. Starts the game with Xvfb and the `mc-runtime-test` mod. The mod joins a
   singleplayer world, waits for chunks, and quits. This loads every client mod
   (including `chat_heads` and `morechathistory`) and exercises world startup,
   but it does not type messages into the chat screen. Xvfb is important:
   without it HeadlessMC enables its LWJGL headless patcher, which Sodium
   correctly refuses to run with.
5. `analyze_minecraft_logs.py` reads `run/logs/latest.log`, any
   `run/crash-reports/crash-*.txt`, and the installed mod jars. It maps stack
   frames and mixin configs back to mod ids and writes:
   - a Markdown report to the GitHub step summary,
   - a `::error` annotation naming the suspected mod and its `.pw.toml`,
   - `artifacts/minecraft-test.json` for machine-readable results,
   - the full logs as the `minecraft-client-test-<version>` artifact.

The job fails when a crash report exists, Fabric/Mixin reports a fatal error,
or `--strict` sees a serious class/linkage error in the log. Caught, non-fatal
exceptions that still have a mod stack frame are reported as warnings with the
mod name (for example `IMBlocker` trying to run `ibus` on a Linux runner) but
do not fail the job.

## Running it manually

```bash
gh workflow run minecraft-client-test.yml -f minecraft_version=1.21.8
```

Watch it with:

```bash
gh run watch
```

## Reproducing the download/staging step locally

```bash
python .github/scripts/download_packwiz_client.py \
  --pack-dir Packwiz/1.21.8 \
  --dest run \
  --dry-run
```

Drop `--dry-run` to download and verify the files. Use
`--exclude 'chat.heads'` (repeatable regex) to stage a pack without a suspect
mod when bisecting a crash.

## Running the log analyzer locally

```bash
python .github/scripts/analyze_minecraft_logs.py \
  --run-dir run \
  --hmc-dir HeadlessMC \
  --pack-dir Packwiz/1.21.8 \
  --strict \
  --json artifacts/minecraft-test.json
```

`--game-outcome failure` can be added to simulate a failed launch step.

## Tests

```bash
python -m unittest discover -s .github/scripts/tests -v
```

The tests build synthetic mod jars and crash reports. They cover:

- a Mixin crash attributed through `chat_heads.mixins.json` and a stack frame,
- a Fabric Loader "Incompatible mods found" error naming a mod,
- nested Fabric API modules,
- Sodium's LWJGL startup failure (the error text itself does not contain the
  word "Sodium"),
- non-fatal caught exceptions (IMBlocker / `ibus`) being reported as warnings,
- `knot//`-prefixed stack frames being attributed correctly,
- optional `ClassNotFoundException` probes at WARN level not being treated as
  failures,
- Packwiz plan/hash/exclude handling.
