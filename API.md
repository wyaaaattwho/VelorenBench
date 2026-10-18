# CLI and agent API

## Unreleased successor contracts

`suite.load/setup/score` remains the task seam. Families own semantic templates
and feature requirements; `support.resolve` generates admission and docs.
Implementation, observation/action profile, historical admission, exact-native
qualification and model success are distinct.

The intentional successor renderer/image default is `visual-ui-v1`, resolved by
`capabilities.default_profile` for ordinary source support, Responses and CLI
matrix/admission. Explicit `visual-motor-v1` is unchanged and narrow. External
adapter declarations are negotiated first; task admission uses the effective
observation mode/profile rather than substituting bundled defaults.

Navigation templates distinguish supply-free rendezvous from
`pickup_rendezvous`: the original `natural_long` layout (N5) authors an apple
and requires its actual pickup and retention as well as arrival. It therefore
requires `movement`, `coordinate_goal` and `pickup`. Its structured/hybrid and
image `visual-ui-v1` interfaces remain implemented; legacy image
`visual-motor-v1` cannot express pickup and is unsupported for this layout.
The navigation family's existing definition validator checks that an explicit
template's requirements match its layout, including arbitrary-ID author tasks.
Renaming a task or selecting a supply-free template cannot bypass this check.
Supply-free layouts, including original N2, do not acquire a pickup dependency.
No original goal, supplies, scorer or native control is changed by this declaration.

`rpg-benchmark support` without a runtime is explicitly source-only and cannot
certify a later binary. `support --config PATH --config-sha256 SHA256` verifies
the actual renderer/helper/content selection (no process launch) and reports
its exact qualification keys. Optional `--observation-mode` and
`--action-profile` select a matrix surface; a profile requires a mode.
`support --manifest PATH` selects author task data instead of a runtime.
The runtime's selected task manifest is used with `--config`.

`rpg-benchmark validate-qualification [--records PATH]` is a local strict
identity/structure check, **not** verification of referenced gameplay artifacts,
promotion, release acceptance or model success. Execution uses only the packaged
`current-qualification.json`; validating another file does not install it.
The1.1.0 candidate packages125 engineering-qualified exact-key records and
retains35 explicit legacy-image exclusions. Bound ordinary admission uses those
records without an unqualified opt-in; source-only support still cannot certify
an unspecified binary. Relative citations resolve against the delivered evidence
sidecar; `tools/verify_evidence.py` checks closure and installed registry equality.
T05/image remains task-engineering only. See CONTRIBUTING.md for
the exact record format and author/curator workflow.

Qualification binds configured task content and its actual family-declared
setup dependencies, not the entire catalog. `mixed.SETUP_REFERENCES` is the
current concrete composition; `suite.setup_dependencies` supplies both real
setup and qualification. Pass the selected `definitions` to `support.resolve`
for author catalogs. The raw `task_manifest_sha256` in support output is
provenance outside the key. Unrelated additions and JSON formatting do not
revoke unchanged task qualification; changed referenced content does.

External `/3` execution preflight now requires `rpg-adapter-preflight/2`.
With `RPG_ADAPTER_STARTUP=1`, stdin contains `rpg-adapter-startup/1` JSON:
exact configuration/backend, `frames` (`null` for headless), independent
absolute `detail_storage`, `model_calls: false`, `native_worlds: false`.
The adapter must actually construct its own client/player/media/detail objects
without connecting or inference, returning the exact `startup`,
`construction_checked: true` and exact `player_package_files`.
Import-only `/1` may be used for installation checks, not execution admission.
Core construction is exercised by `startup.probe_player`; a consumer must
additionally exercise its own real constructor.

Errors retain `source`, `error.type`, `category` and
`occurrence: {clock, value, source}`. Categories distinguish output validation
rejection, provider transport failure, evaluator cancellation, adapter failure
and native refusal. Only an explicitly comparable host-monotonic occurrence can
establish ordering; a foreign clock or host recording timestamp cannot.
Cancellation also needs an observed signal carrying
`caused_by: {kind: evaluator_stop, stop_id, signal}` matching the actual stop
record. `RPG_EVALUATOR_STOP_ID` is provided at launch for the signal handler.
Possessing the token or recording after stop alone is not causal evidence.

Private renderer observations/events identify their shared native
`client-update-counter` domain and session. Observation `event_cursor` bounds
the native event ledger, not the private server journal. Host-received sample
identities distinguish reads sharing a client update; they do not imply fixed
stepping or synchronized effects. Source names and exact transaction joins
remain intact. Image output remains actual PNGs plus allowed transport/control
facts, never private canonical actions, inventory/entity/coordinate dumps.

The public `pickup` action's `uid` is optional: omission lets native select the
nearest pickup in range. Shared evaluator attribution accepts that form only
when the completed same-action native receipt resolves the exact required UID,
actor/session, item and amount, with a server `Collected` event, one compatible
candidate and justified operational collection. An explicit wrong UID still
fails attribution. Inventory gains, target disappearance, submission or an
ambiguous/unproven collection do not substitute for that receipt. This is the
existing native operational association, not stronger server-ground source
proof (`Collected` itself carries no UID/request ID). Provenance-checked private
normalization of image HUD pickup uses the same predicate; no private target
identifiers are added to image tools. Arrival, retention, crafting and authored
world-event/public-readback requirements remain independent and unchanged.

`visual-ui-v1` optionally accepts `modifiers: ["shift"]` or `["ctrl"]` on a
`left_click` at the current PNG's pixels, only when the selected native
`visual_ui.modifiers.left_click` capability advertises it. The stock HUD uses
Shift-click for an individual quantity request and Ctrl-click for its automatic
balance/quantity calculation. Modifiers are pressed/released within that one
fenced gesture, never left held. Omitted/empty modifiers retain ordinary clicking;
combined/unknown modifiers and modifiers on other gestures are refused.
Older pins do not expose this argument. This is not a quantity-by-item-ID or
sale helper: the actual HUD slot, native inventory bounds, prices, trade phases,
confirmations and task reserves still govern effects and scoring.

Resident-dependent setup waits for opt-in, private native settlement-population
readiness before loading its destination. A real architect maintenance must run;
optimistically allocated population counts alone are not completion while due
settlement spawns remain queued. The native day/clock/population targets stay
unchanged. Broad nearest-resident discovery precedes eligibility: actual loaded,
living local Humanoids need native grounded footing and a qualified Human
interaction placement before an RtSim identity is committed. There is no
profession/home/travel-state restriction, NPC freezing or NPC teleporting.
After acceptance, current native bindings must continue to identify that same
RtSim resident. Private reference responses reuse already-qualified proximity
when safe; public player dialogue/UI fences are unchanged. No valid local
resident is an explicit setup failure, not an invented or global substitute.
Placement reuses an already-loaded Human window and actual native terrain
support beside the same resident rather than causing another exact-body overlap
and waiting for collision velocity to decay. Read-only bounded support probes
precede one strict goto; final settled footing and the original5m bound still
apply. Unknown/unloaded terrain and transport failures are not ignored.

Fixture construction stays behind that same common `run` / family setup entry.
Selected flat terrain uses the full native world containing the unchanged authored
coordinates; compact spawn-relative natural templates remain compact. Private
terrain/build queries reject out-of-world synthetic default chunks, even if cached
by a client. An in-world loaded client chunk is not an authoritative write ACK.

The fixture administrator acquires an authenticated, self-only owner-scoped
`fixture_authoring` hold before moving to the first unbuilt voxel. Existing build
permission and area authorization still apply. Shared physics holds only that
administrator's position/velocity; it does not fake grounded support, change
ordinary teleport/physics, or affect the measured actor's tools/goals. One mutation
submission is followed by exact client terrain readback, without replay on unknown
outcomes. Cleanup releases only the acquired owner, restores its captured native
position/velocity and prior build-enabled flag, and revokes/removes only the created
area. Primary and cleanup errors are retained. Family opening/footing checks remain
unchanged. This requires rebuilt matching native source, not an old binary plus
a Python rebind.

Use `rpg-benchmark --help` and `rpg-benchmark run --help` for the exact
arguments. Python imports remain under `scripts.rpg_benchmark`; the common
player tool implementation is `scripts.rpg_sdk_agent`. Core imports do not
require the optional Copilot SDK, aiohttp or the reference DSH package.

## Installed Python API quickstart (no native/model execution)

Run this with the interpreter in which you installed core1.1.0. It loads
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
Non-engineering external entries use `--external-admission PATH` to select a
caller-owned explicit live declaration (`rpg-external-live/1`), without a team
approver. Required fields are `live: true`, nonempty `provider`, `model_calls_admitted: true`,
`pins_sha256`, `adapter_id`, `adapter_sha256`, `model`, `reasoning_effort`,
the complete negotiated `configuration`, and the exact `adapter_config` object.
Keep secrets out of declarations/config files; select credentials through the
adapter's environment/credential interface. See `external.validate_live_declaration`.
Legacy `rpg-external-admission/1` internal experiment files remain readable,
but are not required for public use. Neither form is a credential/service grant
or release acceptance. Configuration changes require a new matching declaration.
`--external-engineering` explicitly identifies synthetic checks.

`rpg-benchmark qualification-keys [--config PATH --config-sha256 SHA]` generates
the actual task/native/code keys for existing variants and declared profiles,
with implementation/refusal and current family-policy modes. No binary is
launched and no qualification evidence is generated. Source-only keys cannot
certify a later native selection; `validate-qualification` validates structure,
not gameplay claims or release acceptance.

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
