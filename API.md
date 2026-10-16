# CLI and agent API

Use `rpg-benchmark --help` and `rpg-benchmark run --help` for the exact
arguments. Python imports remain under `scripts.rpg_benchmark`; the common
player tool implementation is `scripts.rpg_sdk_agent`. Core imports do not
require the optional Copilot SDK.

## Installation and native bindings

- `identity`: executing interpreter, distribution version and Python package
  hashes. It is not native readiness or a Git commit assertion.
- `tasks`: 29 task IDs and scene-manifest hash.
- `support`: backend/task/terrain/modality admission and historical
  qualification metadata.
- `bind-native --native FILE --native-sha256 HASH --assets DIR --out NEW-DIR`:
  creates a renderer binding from a verified `rpg-native-release/1` bundle.
- `check --backend renderer --config FILE --config-sha256 HASH`: validates
  inputs without launching native or model processes.

The binder verifies metadata, binary/source/asset inventories, rejects
traversal, symlinks, overlaps and existing outputs, copies native binaries
and corresponding source into the new binding, and leaves assets external.
The asset root must end in `assets`. The returned `rpg-runtime/1` pins an
independent `rpg-content/1` identity for the **executing installation**.
Rebind after package changes; reinstall rather than moving venv shebangs.
`config/*.example.json` are schematic formats, not valid ready-made pins.

The public checkout has no host-specific protected PID. Process ownership,
PID/start identity and mutation fences remain enforced. If sharing a host
with services, set `protected_processes` explicitly in runtime.json and
recalculate its SHA-256 before running. Each entry is
`{"pid": POSITIVE_INT, "start_ticks": PROC_START_TICKS, "port": TCP_PORT}`.
The hostname-scoped mechanism is retained and tested using a synthetic host.

## Native episode modes

All native episodes require explicit runtime/config hashes and new output
directories. Renderer recordings must be a separate
`.../outputs/recordings/RUN-ID` directory.

- `setup`: fixture validation only, no model call. A setup pass is not a
  benchmark capability result.
- `task-engineering`: disclosed engineering execution, including supported
  synthetic provider checks; not model capability evidence.
- `interface-smoke` / `mechanism`: bounded interface diagnostics; admission
  varies by task. Consult `support`.
- `capability`: scored agent execution with independent validity/evidence
  accounting. Admission alone is not a measured result.

The setup-only command in [README.md](README.md) is the simplest native
example. `--evaluation-horizon` is an explicit wall horizon; if omitted, the
runner stops on success, death or entry error rather than inventing a
runtime quota. Report it and any agent/tool budget. `--seed` must belong to
the manifest (currently `130626853`); T06 additionally accepts `--variant`.

## Configurable renderer agent entry

The real entry is `run --harness responses`. It launches the installed
`responses_entry` tool loop and exposes public `RendererTools`. It does not
contain a task-solving action sequence. It negotiates `--model`,
`--reasoning-effort`, `--observation-mode` and `--action-profile` against an
explicit provider declaration.

**Current CLI limitation:** the bundled live route is `existing-luna`,
declaring `gpt-6-luna` / effort `none`. It requires a separately supplied,
hash-pinned credential-loader module exporting
`_get_oauth_token(allow_interactive_auth=False)`. No loader, account or
credential is provided. This route uses the official service endpoints in
`luna_route.py`; it is not a generic OpenAI-compatible base-URL option.
The optional `[copilot]` SDK does not provision this loader.

For a user who has separately authorized and configured that route, the
actual CLI shape is:

```sh
rpg-benchmark run --backend renderer --mode capability \
  --config /absolute/binding/runtime.json --config-sha256 RUNTIME_SHA256 \
  --scene T05 --terrain natural --harness responses \
  --model gpt-6-luna --reasoning-effort none \
  --observation-mode hybrid --action-profile ordinary-renderer-native-v1 \
  --adapter-config '{"route":"existing-luna","credential_loader":"/absolute/local_loader.py","credential_loader_sha256":"LOADER_SHA256"}' \
  --out /absolute/binding/runs/agent-001 \
  --recordings /absolute/work/outputs/recordings/agent-001
```

This is an interface example, **not an executed result**. Unlike setup, it
would make live model calls and may incur costs. Do not run it without your
own service authorization.

For local engineering without a provider account, the same entry supports
a **synthetic loopback Responses server** using
`--mode task-engineering` and `--adapter-config` of this shape:

```json
{
  "mock_provider": "http://127.0.0.1:18080",
  "provider": {
    "schema": "rpg-responses-provider/1",
    "id": "local-test",
    "tool_calls": true,
    "observation_modes": ["structured", "image", "hybrid"],
    "image_transport": "image-blocks-v1",
    "model_profiles": [{"model": "test-agent", "reasoning_efforts": ["none"]}],
    "default_model": "test-agent",
    "default_effort": "none"
  }
}
```

The server must implement the Responses schema checked by
[`responses.py`](harness/scripts/rpg_benchmark/responses.py).
Synthetic requests are recorded as such and cannot become capability
results. Tests use this seam, not a model/provider campaign.

## Custom external adapters

[`external.py`](harness/scripts/rpg_benchmark/external.py) defines
`Adapter`, `Launch` and `rpg-external-evidence/1`. An adapter declaration uses
`rpg-external-adapter/2` with:

- player interface `rpg-sdk-native-contract/2`, native protocol `0.2`;
- model/effort profiles, modality/action-profile mappings, strict integer
  transport and asynchronous receipt support;
- pinned implementation, source freeze, configuration and public-context
  launch/evidence functions.

The implementation exports `build_launch(context) -> Launch` and
`collect_evidence(context) -> dict`. It receives the goal, public episode
socket, configuration and output locations, not fixture/oracle internals.
See the synthetic
[`mock-adapter`](harness/tests/fixtures/adapters/mock-adapter) and
[`test_external_harness.py`](harness/tests/test_external_harness.py) for the
executable contract, and `capabilities.py` for negotiation.

The CLI has `--harness external --adapter ID --adapter-config JSON`; register
its declaration and hash in runtime/content `external_adapters` first.
Non-engineering external entries require an exact-configuration admission
file (`rpg-external-admission/1`; the legacy approver field is literally
`"approved_by": "A0"`). See `external.Adapter.__init__` for required equality
checks. It is a configuration/evidence gate, not a credential or service grant.
`--external-engineering` explicitly identifies synthetic checks.

**Do not infer renderer support from this generic seam:** renderer episode
selection currently requires `--harness responses`. Arbitrary external
adapter episodes use the inherited headless path, whose portable native
assembly is not delivered here. The Python custom-adapter API and synthetic
tests are usable independently; extending renderer/provider admission is
future engineering, not silently enabled by this release.

## Observations, effects and artifacts

Structured/hybrid use ordinary public native APIs. Image projection is
applied to successful, refused, pending and late tool responses; images are
actual verified PNG bytes transported as Responses `input_image` blocks,
not filenames. Accounting includes historical image blocks actually resent.
Unknown effects remain fenced; duplicate no-effect refusal replay is not a
new mutation. Session, UID and lifecycle references must remain consistent.

The native interface schemas are in `native/frame-contract.json`,
`native/rpg-contract.json` and `native/source/agent_controller/PROTOCOL.md`
(the inherited protocol document includes Chinese text). Private fixture and
oracle state is never ordinary player input.

Per-run artifacts include `result.json`, `trajectory.json`, the native
journal, public tool/receipt streams, entry/provider events, frame files,
dependency hashes, timing and cleanup evidence as applicable. Results retain
the goal score separately from validity/classification, transport errors and
cleanup. Preserve failed/invalid attempts, native-vs-client clock uncertainty
and original results when deriving a corrected score. Never publish secrets
or private oracle data as agent input; audit artifacts before sharing.
