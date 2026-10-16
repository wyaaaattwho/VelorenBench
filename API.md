# CLI and agent API

Use `rpg-benchmark --help` and `rpg-benchmark run --help` for the exact
arguments. Python imports remain under `scripts.rpg_benchmark`; the common
player tool implementation is `scripts.rpg_sdk_agent`. Core imports do not
require the optional Copilot SDK, aiohttp or the reference DSH package.

## Installed Python API quickstart (no native/model execution)

Run this with the interpreter in which you installed core 1.0.1. It loads
the actual task catalog and support schema, without a game, provider or account:

```sh
python -I -B -c '
from pathlib import Path
from scripts.rpg_benchmark import suite
from scripts.rpg_benchmark.renderer_support import support_matrix
tasks = suite.load(Path(suite.__file__).with_name("scenes.json"))
print(sorted(tasks))
print(support_matrix()["renderer"]["C1/natural"])
'
```

For native episodes use the CLI below: it owns fixture isolation, oracle
evaluation and cleanup. For an external Harness, use the `player.tools_for`
seam described below rather than constructing private setup/oracle clients.

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

For your own authorized provider, the `standard-responses` route takes an
explicit HTTPS `/responses` endpoint, environment-variable credential name
and provider capability/model declaration. Set `ADAPTER_CONFIG` to the JSON
shown under [Standard provider configuration](#standard-provider-configuration)
and supply that credential in the process environment. The actual CLI shape is:

```sh
rpg-benchmark run --backend renderer --mode capability \
  --config /absolute/binding/runtime.json --config-sha256 RUNTIME_SHA256 \
  --scene T05 --terrain natural --harness responses \
  --model YOUR_MODEL --reasoning-effort none \
  --observation-mode hybrid --action-profile ordinary-renderer-native-v1 \
  --adapter-config "$ADAPTER_CONFIG" \
  --out /absolute/binding/runs/agent-001 \
  --recordings /absolute/work/outputs/recordings/agent-001
```

This is an interface example, **not an executed result**. Unlike setup, it
would make live model calls and may incur costs. Do not run it without your
own service authorization.

The optional `existing-luna` route instead declares `gpt-6-luna` / effort
`none` and takes
`--adapter-config '{"route":"existing-luna","credential_loader":"/absolute/local_loader.py","credential_loader_sha256":"LOADER_SHA256"}'`.
Its separately supplied, hash-pinned module must export
`_get_oauth_token(allow_interactive_auth=False)`. It uses `luna_route.py`
service endpoints, not a generic base URL. No loader, account or credential is
provided; installing the optional `[copilot]` SDK does not provision a loader.

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
`rpg-external-adapter/3` with:

- player interface `rpg-sdk-native-contract/2`, native protocol `0.2`;
- model/effort profiles, modality/action-profile mappings, strict integer
  transport and asynchronous receipt support;
- pinned implementation, source freeze, configuration and public-context
  launch/evidence functions.
- `runtime.python`: an absolute interpreter in the **adapter's own environment**;
  `runtime.preflight`: its installed `-m PACKAGE.preflight` command arguments;
- `capabilities.backends` (`headless` and/or `renderer`) and, for renderer,
  `capabilities.player_tools: "rpg-player-tools/1"`.

The implementation exports `validate_config(context)`, `build_launch(context) -> Launch` and
`collect_evidence(context) -> dict`. It receives the goal, public episode
socket, configuration and output locations, not fixture/oracle internals.
Configuration validation and isolated dependency/executable preflight run
**before native world creation**. A successful preflight prints a JSON
`rpg-adapter-preflight/1` receipt whose `player_package_files` equals
`runtime.package_files()` of the evaluator. Install the same benchmark artifact
in each adapter environment; do not inject a different venv through `PYTHONPATH`
or `.pth`. Launch uses the declared interpreter with installed modules.
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

Renderer admission now uses backend/task/action-profile and adapter transport
capabilities, not the Responses Harness identity. Use
`player.tools_for(native, evidence, configuration, frames=..., episode=...)`
to obtain the same `Tool`/`Invocation`/`Result` interface for either backend.
Renderer tools reuse verified frame bytes and fail-closed image projection.
`player.content_blocks` serializes **broker** image bytes; an independent
Harness with an attachment store must admit these into its official image
references before its ordinary model loop consumes them. Declaring support
alone does not prove this transport works.

Declarations /1 and /2 remain readable for historical evidence. Executing
external entries must migrate at this single boundary to /3 with preflight.
Legacy built-in entries are retained for compatibility, not newly qualified
external integrations. GameAgent is separately supplied, not distributed or
fully qualified here; its internals are not a benchmark publication gate.
E246 observed Brain/Jev image delivery but an invalid image episode, not
absence of image capability or successful full mixed-modality execution.
Adapters using the bridge must use the installed `rpg-episode-bridge` console
entrypoint, never a `.py` file requiring source executable bits.

## Standard provider configuration

The bundled Responses transport supports this explicit live route:

```json
{
  "route": "standard-responses",
  "endpoint": "https://YOUR_PROVIDER/v1/responses",
  "api_key_env": "MY_PROVIDER_API_KEY",
  "provider": {
    "schema": "rpg-responses-provider/1",
    "id": "my-declared-provider",
    "tool_calls": true,
    "observation_modes": ["structured", "image", "hybrid"],
    "image_transport": "image-blocks-v1",
    "model_profiles": [{"model": "YOUR_MODEL", "reasoning_efforts": ["none"]}],
    "default_model": "YOUR_MODEL",
    "default_effort": "none"
  }
}
```

Set the credential only in the process environment. Choose `--model` and
`--reasoning-effort` from that declaration. No endpoint/account/model discovery,
redirect or fallback occurs. This is a protocol interface, not a provider
compatibility/vision claim or authorization to purchase access. The existing
Luna profile and synthetic loopback route remain separate explicit choices.

## Observations, effects and artifacts

Structured/hybrid use ordinary public native APIs. Image projection is
applied to successful, refused, pending and late tool responses; images are
actual verified PNG bytes transported as Responses `input_image` blocks,
not filenames. Accounting includes historical image blocks actually resent
in user content **and function-call outputs**, as used by official pi-ai.
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

`process_cleanup_complete` records native/player/socket retirement separately
from `harness_evidence_complete` and aggregate `cleanup_complete`. Missing final
evidence is not evidence that a known-gone PID still runs. A normal Agent stop
can be a valid non-success when the evaluator actually observed the final state;
the stopping sample is taken after the entry has exited, not from an observation
that raced its last action. An unobserved final state or unknown mutation remains unknown. Goal/death
precedence is independent of later shutdown errors; validity still records
those errors and does not turn them into successes.

Adapter-provided observed runtime model metadata is preserved separately from
declared profiles. Missing metadata stays `unknown`; the generic evaluator
does not infer which models or executive components an adapter uses.

`task_terminal.classification` captures the measurement-time classification.
The top-level `classification` can refine an ordinary entry exit after collecting
its evidence, distinguishing a normal model stop from provider or entry errors.
An exit alone is not evidence of either error.

The separately installed [official-dsh integration](integrations/deepseek/README.md)
provides a complete reference implementation and standalone binding helper.
It is not a core dependency, and its provider profile is distinct from the
standard Responses route above.
