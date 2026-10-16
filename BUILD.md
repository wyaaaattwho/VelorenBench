# Building from a source checkout

## Python Harness

Source release: **v1.0.1**, with core **1.0.1** and the separate reference
adapter **0.1.0**. Use the tag-pinned checkout in [README.md](README.md).
Native Rust source/mechanics and asset identities are unchanged. New adapter environments
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
not native source/metadata, game assets, historical evidence or materials
inventories. The Git source checkout is the native build entry. Python
build dependencies are pinned; their availability still requires a package
index or predownloaded wheelhouse. See [TESTING.md](TESTING.md).

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
python3 tools/build_native.py --assets /absolute/veloren/assets \
  --out /absolute/new-native-build --check
python3 tools/build_native.py --assets /absolute/veloren/assets \
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

The tool uses an empty compiler target and records command/log digests in
`receipt.json`; a failed prerequisite/compiler command leaves a failed
receipt and log, not a false success. `VELOREN_GIT_VERSION=/f974743d/1788840834`
is the inherited protocol compatibility stamp, not a clean Git status or
binary-equivalence claim. No internal binary/cache bundle is needed.

## Assemble and bind the renderer

```sh
python3 tools/assemble_native.py --fresh-build /absolute/new-native-build \
  --assets /absolute/veloren/assets --out /absolute/native-dist
rpg-benchmark bind-native --native /absolute/native-dist/bundle.json \
  --native-sha256 DESCRIPTOR_SHA256 \
  --assets /absolute/veloren/assets --out /absolute/new-binding
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
