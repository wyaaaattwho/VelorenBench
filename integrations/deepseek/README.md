# Official DeepSeek Harness integration

This separately installed reference adapter is original integration code, not
a vendored official DSH runtime. Its shared execution code has local native/model
integration evidence for structured, hybrid and image-only input. These bounded
checks do not establish broad benchmark coverage, model ability or clean-machine
native readiness. This is the untagged adapter **1.1.0** candidate paired with
core **1.1.0**; see the root [status and source identity](../../README.md).
No registry upload or leaderboard is implied.

The separately installed official DSH Cordis composition, stdio SDK and pi-ai
loop own decisions. This adapter provides the ordinary player-tool broker,
strict integer transport, broker image admission into official attachment
references, process ownership and evidence conversion. It does not contain
a task solver, an evaluator fork or a private GameAgent implementation.

## Scope and prerequisites

- Python **3.13+**, Linux process ownership (`/proc`, `prctl`), Node/npm.
  Local installation checks used Python 3.13, Node 22.22.2, npm 10.9.7.
- Install the **same exact core artifact** in evaluator and adapter venvs.
  This candidate uses `rpg-benchmark-harness==1.1.0`. Matching version text
  is insufficient: preflight compares the core's actual `package_files()`.
- Obtain/build native binaries and external assets separately using the public
  root [`BUILD.md`](../../BUILD.md), including all asset/license prerequisites.
  Run standard `rpg-benchmark bind-native` on your own verified
  `rpg-native-release/1` bundle. Its new `runtime.json` is the input below.
  This package neither supplies native assets nor replaces that build.
- The **only integrated live provider profile** here is the existing
  `gpt-6-luna` / effort `none` account route. Bring your own separately
  authorized access. No account, subscription, credential, generic provider
  compatibility or vision capability is provided. The benchmark's
  `standard-responses` route is a **different** integration.
  Actual backend/model metadata remains `unknown` unless observed.

## Install into independent environments

In these examples, set shell variables to your own absolute local paths:
`INTEGRATION` (this directory), `CORE_WHEEL` (verified core wheel),
`CORE_ENV`, `ADAPTER_ENV`, `DSH_RUNTIME` (new npm directory), `BASE_CONFIG`
(standard bind-native runtime.json), `BINDING` (new registration directory),
`EPISODE_ROOT` (separate new run directory), and `RECORDINGS` (a separate
`.../outputs/recordings/RUN-ID` directory). No historical candidate root or
private freeze parent is required.

```sh
python3.13 -m venv "$CORE_ENV"
python3.13 -m venv "$ADAPTER_ENV"
# Build from the verified candidate source and verify artifact hashes before installing.
"$CORE_ENV/bin/python" -m pip install --no-deps "$CORE_WHEEL"
"$ADAPTER_ENV/bin/python" -m pip install --no-deps "$CORE_WHEEL"
"$ADAPTER_ENV/bin/python" -m pip install --require-hashes -r "$INTEGRATION/requirements-build.lock"
"$ADAPTER_ENV/bin/python" -m pip install --require-hashes -r "$INTEGRATION/requirements-hashed.lock"
"$ADAPTER_ENV/bin/python" -m pip install --no-deps --no-build-isolation "$INTEGRATION"
"$CORE_ENV/bin/python" -m pip check
"$ADAPTER_ENV/bin/python" -m pip check
mkdir "$DSH_RUNTIME"
cp "$INTEGRATION/dsh_external_v1/data/package.json" "$INTEGRATION/dsh_external_v1/data/package-lock.json" "$DSH_RUNTIME/"
(cd "$DSH_RUNTIME" && npm ci --registry=https://registry.npmjs.org/)
"$ADAPTER_ENV/bin/python" -I -B -m dsh_external_v1.preflight --runtime "$DSH_RUNTIME"
```

`requirements.lock` is the unchanged original **version-only** dependency
list; `requirements-hashed.lock` pins those same versions with public PyPI
distribution hashes (wheels and source distributions). Build requirements
are separately hash locked. Installation may run normal upstream build/npm
lifecycle scripts: use an appropriately isolated environment and review terms.
Only the minimal npm manifest/lock is shipped, not `node_modules`. The lock
retains the original complete dependency resolution, including pi-ai 1.2.0;
only its local root package name/version were renamed. Package identity,
registry tarball URLs, integrity and terms were checked against public npm
metadata; no guessed Git installation URL is required.

## Bind: no worlds, no model calls, no credential access

Run the single helper with the **evaluator's** interpreter. Its implementation
uses only stdlib and core; importing the installed launch/collect shim likewise
does not pull aiohttp or an optional Copilot SDK into the evaluator.

```sh
DSH_BIND=$("$ADAPTER_ENV/bin/python" -I -c 'import pathlib,dsh_external_v1; print(pathlib.Path(dsh_external_v1.__file__).with_name("binding.py"))')
BASE_SHA=$(sha256sum "$BASE_CONFIG" | cut -d ' ' -f 1)
"$CORE_ENV/bin/python" -I -B "$DSH_BIND" \
  --config "$BASE_CONFIG" --config-sha256 "$BASE_SHA" \
  --python "$ADAPTER_ENV/bin/python" --runtime "$DSH_RUNTIME" \
  --out "$BINDING" --output-root "$EPISODE_ROOT" \
  --observation-mode structured --action-profile ordinary-renderer-native-v1 \
  --admit-model-calls
CONFIG_SHA=$(sha256sum "$BINDING/runtime.json" | cut -d ' ' -f 1)
"$CORE_ENV/bin/rpg-benchmark" check --backend renderer \
  --config "$BINDING/runtime.json" --config-sha256 "$CONFIG_SHA"
```

The helper validates the original runtime/content hashes through public
`runtime.load_runtime`, validates installed dependencies/entry/plugin, then
writes **new** `adapter-freeze.json`, `runtime-freeze.json`,
`deepseek-harness.json` (`rpg-external-adapter/3`), `content.json`, `runtime.json`
and a binding receipt. The implementation resolves to installed
`dsh_external_v1/adapter.py`, not an unshipped checkout file. The only freeze
parent is this new installation's own runtime inventory. It hashes the actual
npm manifest/lock and complete installed `node_modules` bytes, including the
official internal `.bin` links. It never copies that runtime. Paths, hashes,
preflight command, strict transport, capability mappings and exact core
identity remain enforced before any world allocation. Failed final validation
may leave a partial new binding; do not reuse it, choose another new output.
The base binding and protected-process entries remain unchanged.
The final constructor preflight uses distinct binding-owned `preflight-frames`
and `preflight-details` paths even for structured observations; this is storage
construction only, not native allocation or a model request.

`--admit-model-calls` is an explicit local opt-in to write `admission.json`.
It writes a public `rpg-external-live/1` declaration with `live: true`, explicit
`provider`, exact negotiated configuration and exact adapter options. No team
approver identity is required. This is **not provider authorization or a service
grant**. The helper does
not contact anyone or obtain permissions. Without that option it writes no
admission and its dependency checks use an inert loopback engineering
configuration; no socket connection is made. A live run still requires an
admission matching runtime hash, declaration hash, model, effort and the
complete negotiated configuration. Rebind into a new directory after any
code/dependency/runtime/configuration change; do not hand-edit frozen files.

## Explicit supported examples

Consult the public `rpg-benchmark support` matrix. These examples describe
**admitted configurations**, not all 29×3 support or a guarantee for a new machine:

| Task | Terrain | Observation | Action profile |
| --- | --- | --- | --- |
| C1 | natural | structured or hybrid | ordinary-renderer-native-v1 |
| N1 | flat | image | visual-motor-v1 |

For hybrid C1, bind a **new directory** with `--observation-mode hybrid`.
For image N1, bind another new directory with `--observation-mode image
--action-profile visual-motor-v1`. Do not reuse another configuration's
admission. The `/3` declaration preserves the existing complete transport
capabilities/mode mappings; declarations alone do not expand the task matrix.

The unchanged credential reader accepts a separately supplied authorized
`COPILOT_GITHUB_TOKEN` or an existing user's
`~/.cache/github-copilot-client/oauth.json` containing `oauth_token`.
It disables interactive login and writes no credential cache. Set up access
outside this package; do not put credentials in declarations, command lines,
source, examples or shared artifacts. No plaintext credential example is
provided. Do not run the following without your own authorization; it starts
native processes and can incur model costs:

```sh
ADAPTER_CONFIG=$("$CORE_ENV/bin/python" -c 'import json,sys; print(json.dumps({"runtime":sys.argv[1]}))' "$DSH_RUNTIME")
"$CORE_ENV/bin/rpg-benchmark" run --backend renderer --mode capability \
  --config "$BINDING/runtime.json" --config-sha256 "$CONFIG_SHA" \
  --scene C1 --terrain natural --seed 130626853 --harness external \
  --adapter deepseek-harness --adapter-config "$ADAPTER_CONFIG" \
  --model gpt-6-luna --reasoning-effort none \
  --observation-mode structured --action-profile ordinary-renderer-native-v1 \
  --external-admission "$BINDING/admission.json" \
  --out "$EPISODE_ROOT/c1-001" --recordings "$RECORDINGS"
```

For N1 change `--scene N1 --terrain flat --observation-mode image
--action-profile visual-motor-v1` and use its exact separately generated
binding/admission/hash and new output/recordings locations. Native `setup`
is a separate prerequisite/fixture check, not a model capability result.
Evaluation horizon, when selected, must be explicit and reported.

## Focused tests

### Lossless public content and integer arguments

The common `Result.text_result_for_llm` is the authoritative public JSON text.
The broker validates its JSON syntax and finite numbers, then carries that text
unchanged into the official tool renderer. It does **not** parse observations
into a second JavaScript numeric payload. Finite signed zero (for example,
`[1.0, -0.0]`) and complete 64-bit integer decimals therefore remain recoverable
in tool content and session history, including nested public fields. Consumers
parsing that text must use an exact-integer JSON reader, not JavaScript numbers.
Malformed/nonfinite JSON remains an explicit transport failure; no official
snapshot guard or native validation is relaxed.

For schema-declared integer argument fields whose native domain can exceed the
JavaScript safe range, the provider schema accepts either a safe numeric integer
or a **canonical decimal string** (e.g. `"5340282195752818211"`). Copy large
identities as decimal strings: never round, clamp, use exponent notation, or
guess another identity. Python decodes only those declared integer locations
back to exact integers and applies the original native schema, including bounds,
action unions, actor/lifecycle and ownership fences. Unsafe numeric integers
are refused before submission. Ordinary string/number/boolean/null arguments
keep their original types; bounded integer fields such as durations remain
integers. This is wire encoding, not a new native action or a fence refresh.
For converted calls, evidence retains the observed wire arguments, native schema
and decoded arguments. The collector replays the same frozen decoder and checks
the mapping before joining exact native receipts; it does not mistake a lossless
decimal encoding for a different model intent.

Model images still use common typed image bytes admitted by the official
attachment store and rendered as official image references, not base64 text.
The common image-only projection is unchanged for successes, pending receipts,
refusals and repeated history; no evaluator/private observation is added.

```sh
"$ADAPTER_ENV/bin/python" -I -B -m unittest discover -s "$INTEGRATION/tests" -p test_binding.py -v
"$ADAPTER_ENV/bin/python" -I -B -m unittest discover -s "$INTEGRATION/tests" -p test_content_codec.py -v
mkdir /absolute/new-rpc-test-output
DSH_RPC_TEST_OUTPUT=/absolute/new-rpc-test-output \
  "$ADAPTER_ENV/bin/python" -I -B -m unittest discover -s "$INTEGRATION/tests" -p test_rpc_lifecycle.py -v
# Choose an unused output root; these are synthetic loopback peers, not live calls.
DSH_OBSERVATION_TEST_OUTPUT=/absolute/new-observation-test-output \
  DSH_RUNTIME="$DSH_RUNTIME" \
  "$ADAPTER_ENV/bin/python" -I -B -m unittest discover -s "$INTEGRATION/tests" -p test_observation_transport.py -v
```

The observation tests run the actual installed official Cordis/plugin/stdio/
pi-ai loop against a synthetic local SSE peer and synthetic public native
receipts. They cover signed-zero geometry, exact large observation/argument
integers, official image bytes/references, image-only pending/refusal/history,
unsafe-number refusal and stale-fence non-retry. All owned runtime children
are retired. These tests are not native qualification or model measurements.

The eleven preserved synthetic RPC regressions do not start worlds or model
calls. Staging verification additionally tests isolated evaluator imports,
installed/source byte identities, real official runtime plugin imports,
standalone registration/config loading and pre-world negative cases.
The initial staging reused a read-only npm deployment. A later separate fresh
`npm ci` installation from these locks and its no-model preflight passed;
publication checks reuse that runtime read-only, not a new npm installation
or native/GPU deployment. See [PROVENANCE.md](PROVENANCE.md) for the source and
verification boundaries.
