# Building from a source checkout

## Untagged release candidate 1.1.0

This candidate is **not** v1.0.1 and has no release commit/tag. Python packages
use `1.1.0` for both core and reference adapter. This is not release acceptance.
The final local seal is tests/docs/registry-only relative to the image-intent
successor: production Python and native code identities are unchanged.
Its125 exact qualified keys remain usable;35 legacy-image exclusions and the
T05/image task-engineering restriction remain. No native rebuild is required
for this data-only seal. The delivery includes the byte-identical selected
native descriptor/binaries and entire corresponding source, not an asset tree.
`verify_evidence.py --root evidence --native native-bundle/bundle.json
--source-manifest source/source-manifest.json` works after relocation and links
the GPL evidence object to the actual included source. Keep all upstream notices;
the18 original game PNGs remain evidence subject to upstream asset terms for
any future authorized external distribution. This local seal is not publication.
`native/selection.json` binds the deliberately modified current
native tree/manifest, while `source.json.successor_baseline` and the existing
benchmark/upstream pins retain their historical meanings. Do not substitute
distribution identity for a benchmark/native baseline or invent a Git commit.
After native edits run `tools/refresh_native.py --write`, and after final source
edits run `tools/package_source.py --write`; inspect both generated inventories.
The existing build/assembly/bind commands below use the selected successor
inventory. They require the same exact legally obtained external assets and
Linux/aarch64 toolchain. No source/binary/license/platform closure or real
native task qualification follows from an inventory check or Python install.
No GameAgent checkout, account, local model or private headless binding is a
standalone package dependency. Original source-release receipts below remain
historical; do not overwrite their builds/bindings/results.

Successor `generic-release-2` adds genuine middle-button HUD injection. Rebuild
from the newly selected native source, assemble a new bundle, then bind the
matching Python package; an older binary cannot qualify the new gesture.
Python advertises `middle_click` only when the actual renderer-ready capability
includes it. The public map input uses its existing M3 waypoint binding, not a
structured coordinate command. Native integration qualification is still separate.

## Python Harness

Frozen historical source release: **v1.0.1**, with core **1.0.1** and reference
adapter **0.1.0**. Those identities are not this candidate. For this candidate,
build/install core and adapter **1.1.0** from the same verified checkout.
Native source changes require a newly assembled bundle; asset identities remain pinned. New adapter environments
are independent installations: install the same benchmark wheel in each,
then the adapter's declared dependency lock and installed entry module.
Run the adapter's no-model preflight before a native episode. Core installation
must not accidentally supply an external Agent's imports. Process adapters
using the bridge require `rpg-episode-bridge` in their interpreter's `bin` directory.
Do not chmod source scripts or join site-packages with `PYTHONPATH`/`.pth`.

Python >=3.13 is required; core runtime dependencies are empty. From the
repository root:

```sh
python3.13 -m venv --copies /absolute/build-env
/absolute/build-env/bin/python -m pip install --require-hashes -r requirements-build.lock
/absolute/build-env/bin/python -m build --no-isolation --outdir /absolute/python-dist
```

Install the resulting wheel with `pip install --no-deps FILE.whl`. The
wheel and sdist contain the MIT Python Harness/docs/tools/tests as applicable,
not native source/metadata, game assets, the separate evidence sidecar or materials
inventories. The Git source checkout is the native build entry. Python
build dependencies are pinned; their availability still requires a package
index or predownloaded wheelhouse. See [TESTING.md](TESTING.md).

Build the reference adapter separately with the same build interpreter:
`/absolute/build-env/bin/python -m build --no-isolation --outdir /absolute/adapter-dist integrations/deepseek`.
Install the same exact core wheel in the independent adapter environment, then
its hash-locked dependencies and adapter wheel. Run `python -m pip check` in
both environments; matching version text never replaces preflight byte identity.

## Selected native platform and prerequisites

Only **Linux/aarch64** is selected. Full native source compilation was not
rerun for this export; other platforms and a clean machine are unverified.
The source and selected commands are preserved, not claimed portable merely
because the Python wheel installs.

Required toolchain: rustup **nightly-2026-06-13**, observed rustc
`1.98.0-nightly (654079540 2026-06-12)`, host
`aarch64-unknown-linux-gnu`. Check availability with:

```sh
rustup toolchain install nightly-2026-06-13
rustup run nightly-2026-06-13 rustc -Vv
rustup run nightly-2026-06-13 cargo -Vv
```

Provision your system packages explicitly. On Debian/Ubuntu-like aarch64
systems, typical package names include `build-essential`, `gcc-aarch64-linux-gnu`,
`g++`, `cmake`, `make`, `pkg-config`, `libasound2-dev`, `libudev-dev`,
`libvulkan1`, `xvfb` and `x11-utils`. A working Vulkan ICD/GPU driver and
runtime glibc/libstdc++/ALSA/libudev are required. The aarch64 source config
selects `aarch64-linux-gnu-gcc`; shaderc is built from source. Package names
are guidance, not a verified complete clean-machine recipe or GPU guarantee.
No tool installs global system packages for you.

## External assets: required, not distributed

Obtain Veloren assets legally from the upstream project and follow their
per-file licenses/credits. The inherited source lineage is documented in
[native/UPSTREAM.md](native/UPSTREAM.md). Use `native/assets-manifest.json` to
verify the **exact selected asset tree**; an arbitrary current asset checkout
is not an equivalent input. The inventory is not a download/rights grant.
Exact historical asset availability is not proven by this source export.
For a Git/LFS upstream checkout, fetch the corresponding upstream revision
and its LFS objects using upstream instructions; compare the actual files,
not LFS pointer stubs. Extra or changed files cause assembly/binding refusal.

### Reconstruct the three benchmark entity configurations

The selected 10,579-file inventory includes three benchmark-authored
exact-male-body derivatives that are **not stock upstream filenames**.
`tools/prepare_assets.py` reconstructs them locally from the three pinned
stock configurations; no held benchmark asset directory is required.
All stock files must match `native/assets-manifest.json`. Only the three
derivative entries may be absent; any present derivatives must match too.

Paths below are relative to `common/entity/`:

| Stock input | Generated output | Existing output SHA-256 |
| --- | --- | --- |
| `wild/aggressive/green_legoom.ron` | `benchmark/male_green_legoom.ron` | `c7be31b1bfab963903bc1ee5c5ed78b31af5b85f1c7b12761b64fea876f80435` |
| `wild/aggressive/ogre.ron` | `benchmark/male_ogre.ron` | `94c4e8c1b881002b9627a1475efa3935456d1fcd1298f2adb02e6e2016941a89` |
| `wild/aggressive/wolf.ron` | `benchmark/male_wolf.ron` | `9c40d12de1ad5cce59055760a366501779fe0763d82725c70152aa062ef841c3` |

```sh
python3 tools/prepare_assets.py --assets /absolute/stock/assets \
  --out /absolute/benchmark-assets --check
python3 tools/prepare_assets.py --assets /absolute/stock/assets \
  --out /absolute/benchmark-assets
```

`--check` validates every input hash, computes the three derivatives and
verifies their exact hashes and the resulting complete inventory, without
creating output. Preparation copies the stock files into a **new external**
`/absolute/benchmark-assets/assets`, adds the three derivatives, verifies all
10,579 hashes again and writes `/absolute/benchmark-assets/receipt.json`.
The final tree digest remains
`cec450b9d592af4c1ef60db1a9a77112957a15c7449aa3cfcea842a4a562e8a8`.
Existing outputs, overlapping paths, symlinks, extra/altered stock files and
wrong generated hashes are refused. Source assets are never changed.

Only the body selection and historical whitespace/final-newline formatting
change. Original names, alignment, loot and loadouts remain intact, including
Green Legoom's inherited loadout and Ogre/Wolf's native `FromBody` equipment
construction. These are the existing fixtures, not new tasks or eased goals.
The public tool supplies original transformation instructions, **not copied
upstream configurations or art**. Its MIT license does not relicense generated
files: retain upstream terms and credits, and review rights before sharing a
locally materialized asset tree. See [NOTICE.md](NOTICE.md). The tool cannot
obtain missing stock files or establish redistribution rights for you.

The full selected source manifest includes one compile-time resource:
`assets/voxygen/audio/null.ogg`. It is **not** in this public checkout.
The build tool copies its exact hash-checked bytes from
`--assets DIR/voxygen/audio/null.ogg` into the new isolated build source.
All 1158 selected files are verified before compilation; the repository's
1157-file native source subset is not mislabeled as the full selected tree.
If you cannot obtain the pinned resource/assets, native assembly is blocked;
do not synthesize substitutes, weaken hashes or claim an equivalent build.

## Source validation and build

Choose new output prefixes outside this checkout. Paths must be absolute
without symlinks or `..`, and outputs must not overlap any inputs.

```sh
python3 tools/build_native.py --assets /absolute/benchmark-assets/assets \
  --out /absolute/new-native-build --check
python3 tools/build_native.py --assets /absolute/benchmark-assets/assets \
  --out /absolute/new-native-build --fetch --jobs 8
```

`--check` verifies source/resource identity and output paths only: no
compiler, downloads, native processes or output creation. `--fetch`
explicitly runs `cargo fetch --locked --target aarch64-unknown-linux-gnu`
in the staged source and fresh Cargo home. It needs public network access;
dependency availability and the requested nightly remain external
prerequisites. All three subsequent builds use `--offline --locked`.
Alternatively supply `--cargo-sources /absolute/downloaded-CARGO_HOME`
containing registry/git source downloads; target artifacts are never copied.
Use `--cargo` and `--rustup-home` for nonstandard rustup locations.

Selected compilation commands, from the staged source:

```sh
cargo build --offline --locked -p veloren-server-cli -j 8
cargo build --offline --locked -p veloren-client --features bin_bot --example agent_bridge -j 8
cargo build --offline --locked -p veloren-voxygen --bin veloren-voxygen --no-default-features --features simd,shaderc-from-source -j 8
```

The tool defaults to an empty compiler target and records command/log digests in
`receipt.json`; a failed prerequisite/compiler command leaves a failed
receipt and log, not a false success. `VELOREN_GIT_VERSION=/f974743d/1788840834`
is the inherited protocol compatibility stamp, not a clean Git status or
binary-equivalence claim. No internal binary/cache bundle is needed.
For an owned local cache, `--target-cache /absolute/previous/target` makes an
independent copy, runs all the same selected Cargo builds/invalidation, and records
`compiler_artifacts_inherited: true` and its input path. Assembly preserves that
provenance and still verifies exact selected source, all build logs and binary
hashes; cached builds are not empty-target or clean-machine claims.

## Assemble and bind the renderer

```sh
python3 tools/assemble_native.py --fresh-build /absolute/new-native-build \
  --assets /absolute/benchmark-assets/assets --out /absolute/native-dist
rpg-benchmark bind-native --native /absolute/native-dist/bundle.json \
  --native-sha256 DESCRIPTOR_SHA256 \
  --assets /absolute/benchmark-assets/assets --out /absolute/new-binding
rpg-benchmark check --backend renderer \
  --config /absolute/new-binding/runtime.json --config-sha256 RUNTIME_SHA256
```

Assembly requires a successful matching build receipt, unchanged source,
binaries/logs and the complete exact external asset inventory. It emits the
existing `rpg-native-release/1` schema, preserving GPL corresponding source
and relative metadata. It copies no external assets except the compile-time
resource already in corresponding build source; such **local** assembled
bundles are not the public source export and require their own rights review
before sharing. Fresh binary hashes belong to that build, not historical
ones. The binder pins the executing Python installation and leaves runtime
assets external. `check` and binding launch no game.

Use the setup example in README only after prerequisites pass. This source
preparation did not run a full Rust build, GPU test, native episode, clean
machine deployment or model campaign. Retained final008 native/model checks
reused existing Linux/aarch64 binaries and assets; see [PROVENANCE.md](PROVENANCE.md).
Dependency/toolchain retrieval,
actual compilation and external asset/driver availability remain unverified
here; source assembly and synthetic packaging tests are narrower checks.
