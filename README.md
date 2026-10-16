# VelorenBench

**An external-agent benchmark harness for long-horizon RPG tasks in
[Veloren](https://veloren.net/).** VelorenBench evaluates navigation, resource
use, crafting, combat, support, and NPC interactions against native game
effects—not an agent's claim that it succeeded.

## Status and scope

This is the **1.0.0 initial source release**, derived from the locally frozen
benchmark **1.0.0/build001**. It contains 29 task definitions (17 original and
12 additions) and 37 raw task/terrain combinations. These are **not** 37
supported combinations in every observation mode.

The local freeze established bounded engineering functionality, not a model
evaluation campaign. **There are no 1.0 model results or leaderboard.** This
public export changes packaging, documentation and deployment configuration;
it is not byte-identical to the frozen local artifacts. See
[provenance](PROVENANCE.md) and the [benchmark card](BENCHMARK_CARD.md).

### Features

- **Structured, image and hybrid observations**, with explicit task-specific
  admission and no hidden structured fallback for image-only agents.
- A **generic external-agent interface** with action/receipt accounting,
  negotiated model/effort/modality profiles and evaluator-owned termination.
  The harness is not a hardcoded task solver; private qualification
  controllers are not distributed.
- **Native outcomes** for inventory, equipment, damage, healing, trade,
  dialogue and quest settlement, with actor/session identity checks.
- Separate **goal success, measurement validity, transport and cleanup**
  accounting; reproducible manifests and per-episode artifacts.
- A Python core with **zero third-party runtime dependencies**, requiring
  **Python 3.13 or newer**. Native game execution is a separate setup.

## Task families

| Family | Task IDs |
| --- | --- |
| Crafting | C1, C2, C3, C3U, C4 |
| Combat | B1–B3 |
| Navigation | N1–N7 |
| Mixed objectives | X1–X2 |
| Information seeking / route change | T01–T02 |
| Crafting and recovery | T03–T05 |
| Combat builds and companion support | T06–T09 |
| Trade and native contracts | T10–T12 |

Total: **29 definitions**. T06's sword/bow/staff/sceptre conditions are not
four separate tasks. Exact goals are in the [task catalog](TASKS.md);
[support](SUPPORT.md) lists backend, terrain, modality and run-mode limits.
Renderer structured/hybrid support comprises 26 scored cells; image-only
scored support is N1/flat. T05 image has an engineering-only scored path, not
capability admission. Original headless compatibility is inherited; the
portable native assembly in this release is **renderer-only**.

## Quickstart: Python source checkout

```sh
git clone https://github.com/wyaaaattwho/VelorenBench.git
cd VelorenBench
python3.13 -m venv --copies .venv
.venv/bin/python -m pip install .
.venv/bin/rpg-benchmark identity
.venv/bin/rpg-benchmark tasks
.venv/bin/rpg-benchmark support
.venv/bin/rpg-benchmark run --help
```

The distribution name remains `rpg-benchmark-harness` and the CLI is
`rpg-benchmark`. The commands above inspect Python installation and support;
they do not download game assets, start a game, call a model, or validate
native readiness. Build isolation downloads the pinned Python build tools;
the installed core has no runtime dependencies. See [testing](TESTING.md)
for hash-locked build/test environments and offline installation.

### Native setup and a setup-only episode

Read [BUILD.md](BUILD.md) first. Native execution requires the selected
Linux/aarch64 Rust toolchain, system libraries, a working GPU/Vulkan path,
Xvfb, and **separately and legally obtained Veloren assets** matching the
asset manifest. Assets, native binaries and compiler caches are not included.
The build and assembly tools work from this source checkout; they do not
require an internal prebuilt bundle.

After building and assembling a local bundle, bind it to the executing
Python installation. From the checkout root, activate the environment
created above so the CLI commands use that installation:

```sh
. .venv/bin/activate
rpg-benchmark bind-native \
  --native /absolute/native-dist/bundle.json \
  --native-sha256 DESCRIPTOR_SHA256 \
  --assets /absolute/veloren/assets --out /absolute/new-binding
rpg-benchmark check --backend renderer \
  --config /absolute/new-binding/runtime.json --config-sha256 RUNTIME_SHA256
rpg-benchmark run --backend renderer --mode setup \
  --config /absolute/new-binding/runtime.json --config-sha256 RUNTIME_SHA256 \
  --scene T05 --terrain natural --harness responses \
  --observation-mode structured \
  --out /absolute/new-binding/runs/setup-001 \
  --recordings /absolute/work/outputs/recordings/setup-001
```

Replace hash placeholders with the values printed by the tools. Output
directories must be new and separate from protected inputs. `check` does
not launch anything; `run --mode setup` **does launch native processes** but
does not call a model. It validates a fixture, **not agent success or a model
result**. This example was not executed as gameplay for the source export.

### Connect an agent

[API.md](API.md) documents the actual CLI/API, configurable agent entry,
provider declaration and external adapter protocol. Agents receive a public
goal and ordinary observations, not the private oracle. The bundled renderer
entry uses the Responses tool loop; its current live provider binding is
narrow, not an arbitrary provider-URL switch. Custom external adapters and
their admission/evidence requirements are documented separately. No
credentials, subscriptions or model calls are supplied or implied.

## Methodology and reporting

Each episode constructs and validates a seeded native fixture, exposes only
the permitted player interface, and evaluates effects against private oracle
evidence. **Oracle goal satisfaction is not sufficient for a valid model
result**: model/tool/native attribution, transport, timing, entry and cleanup
must be reported independently. Setup failures and unknown effects must not
be relabelled as agent failures or successes.

Report at least: task/terrain and supported-cell denominator; attempts and
valid episodes; seeds and fixture configuration; source/build and scorer
identities; observation/action profiles; adapter version; declared model and
effort (actual runtime metadata separately, `unknown` if unavailable); tool
budget and evaluation horizon, including when unset; success and validity
definitions; invalid/setup/transport/cleanup counts; and aggregation/retry
policy. Do not compare different modalities or drop failed setup attempts
without disclosing the changed denominator.

See [BENCHMARK_CARD.md](BENCHMARK_CARD.md) for the evaluation boundary and
[API.md](API.md) for artifact details. The source snapshot includes tests and
portable identity metadata, not historical runs or a fabricated results table.

## Reproducibility and limitations

- Native Rust source is preserved from the selected tree; one compile-time
  audio asset is excluded and must be supplied externally. The full selected
  inventory and exported-source identity are distinguished explicitly.
- T12 hybrid has historical native success. Its structured qualification
  includes corrected scoring/replay of preserved native evidence, **not a
  fresh native success**; original false/interrupted/unknown records remain
  unchanged.
- Linux/aarch64 is selected. Other systems, GPUs and clean-machine setup
  are unverified. Earlier local setup refusals remain a reliability limit.
- Native variable-dt simulation, frame/action ordering and same-host clock
  mapping have uncertainty. Renderer and headless timing/worlds are not
  asserted equivalent. Broad image-only task coverage is not established.
- Source/package tests do not establish model ability, full build closure,
  deterministic replay, asset rights or cross-platform support.

## Repository layout

```text
harness/scripts/       Python harness, CLI, player interface and adapters
harness/tests/         Unit/contract tests and synthetic/catalog fixtures
native/source/         Selected modified Veloren GPL corresponding source
native/*.json         Source, interface and external-asset identity metadata
tools/                Source inventory, build, assembly and installation tools
config/               Portable schema examples, not ready-to-run bindings
```

## Contributing, licensing and citation

Read [CONTRIBUTING.md](CONTRIBUTING.md) for lightweight contribution guidance.
Keep task goals, scorer predicates and qualification claims explicit; include
focused tests and disclose any semantic changes.

The original Harness is **MIT**; adapted GameAgent code retains its existing
MIT attribution in [LICENSE](LICENSE). The modified native Veloren tree is
**GPL-3.0-or-later**, with its original [LICENSE](native/source/LICENSE).
External assets and dependencies have independent terms and are not
relicensed by this repository. See [NOTICE.md](NOTICE.md).

Thanks to the Veloren project and its contributors, and to the GameAgent
codebase from which parts of the harness were adapted. This is an independent
benchmark project, not an official Veloren release.

For now, cite the **VelorenBench repository**, version and the exact commit
used: <https://github.com/wyaaaattwho/VelorenBench>. Include access date and
configuration in research reports. No associated paper or DOI is claimed.
