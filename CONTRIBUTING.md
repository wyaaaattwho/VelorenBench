# Contributing

## Task authoring in the untagged 1.1.0 candidate

The evaluator, native private owner, ordinary measured actor, renderer, tools and
external adapter are shared. Do not add task-ID branches to any of them.
`scenes.json` and the original transaction/contract predicates remain historical
fixtures; an author example does not become a new scored release task.

### Data-only existing-template variant

`harness/scripts/rpg_benchmark/examples/combat-variant.json` is a separate,
arbitrary-ID `combat/single_creature` example using existing exact-body assets.
The template owns setup and the unchanged contributor/last-damage/death/survival
scorer. It starts unqualified and cannot borrow B1's qualification or model result.

With this candidate installed (not a historical site-packages installation):

```sh
python -B tools/author_task.py --out /absolute/new-catalog.json
rpg-benchmark tasks --manifest /absolute/new-catalog.json
rpg-benchmark support --manifest /absolute/new-catalog.json
python -B -m pytest -q -p no:cacheprovider harness/tests/test_generic_release.py
```

To run it later against an authorized public-renderer build, use a NEW runtime
binding: set its `task_manifest` to the relative/absolute catalog path, set the
content manifest's `task_manifest` to that file's SHA-256, and update the runtime's
`content.sha256`. The binding validates the entire family data, seed list and
exact native assets before allocating a world. Select the example ID with the
existing `run --scene`, `--backend renderer`, `--mode task-engineering`,
`--unqualified-engineering`, and the appropriate ordinary/visual profile.
No runner, renderer, player tool or adapter ID edits are involved. Do not overwrite
an existing binding/catalog or run a model merely to validate author data.

### Native-bound engineering qualification (data, not task-ID policy)

The intentional successor renderer/image default is `visual-ui-v1`. Omitted
flags, ordinary Responses declarations and the source matrix share that default.
Explicit `visual-motor-v1` remains motor-only; it cannot admit crafting/inventory
tasks. External adapters keep their own declared defaults. Admission negotiates
their effective mode/profile first and rejects incompatibility before allocation;
there is no silent UI upgrade or structured/hybrid fallback.

`harness/scripts/rpg_benchmark/current-qualification.json` is the only current
record store, shipped as package data. Its schema is
`velorenbench-engineering-qualification/1`, with exactly `schema` and `records`.
The current1.1.0 candidate integrates125 accepted evidence-backed engineering
records on the exact selected code/native keys, with35 explicit legacy-image
refusals. This is production engineering admission, not final release acceptance
or model capability. Existing current-key ordinary runs need no unqualified
opt-in; new author IDs/content/native/profile selections still require their
own evidence. The final evidence sidecar resolves relative citations independently
of site-packages; the stdlib verifier checks closure and installed registry bytes.
**Production records require genuine, attributable evidence on exact identities.**
Read `validate-qualification` and bound `support` for current registry/readiness;
source-only support and synthetic tests cannot qualify a native binary.

Generate rather than transcribe the current keys with:
`rpg-benchmark qualification-keys --config /absolute/runtime.json --config-sha256 SHA`.
This includes every authored task/terrain/mode, all T06 sword/bow/staff/sceptre
selected variants, and both declared image profiles. Unsupported legacy-profile
combinations retain their actual reason; they are not new scored tasks.
The output creates no evidence or positive qualification. Curate records using
the existing schema, cite hashed operation/effect/scorer evidence and dependency
reuse mapping, and run `validate-qualification --records PATH` before packaging.
Do not copy a key from another binary, code revision, variant or profile.
`qualification.json` and `renderer_support.historical_policy()` retain historical
receipts/policy unchanged for audit, not current-native proof. Do not copy old
qualification, manufacture positives from screenshots/unit tests, or relabel
synthetic test records as native integration.

1. Inspect the actual configured task and selected native without starting it:

   ```sh
   rpg-benchmark support --config /absolute/runtime.json --config-sha256 SHA256 \
     --observation-mode image
   ```

   Select that task/terrain's `qualification_key` verbatim. It binds the
   configured task content (including ID, template, requirements and effective
   variant), defined terrain, backend, observation mode and action profile.
   It also binds `harness_code_sha256`: the canonical SHA-256 map digest of all
   installed `scripts` Python files plus `native_features.json`. This is not the
   distribution's entire Python source manifest. Evidence data and source/bundle
   manifests are excluded from that code digest to avoid a self-hashing cycle.
   Native identity includes the renderer source tree/manifest, assets
   tree/manifest, executable, server, interface and build receipt hashes, the
   matching fixture helper's executable/source/build hashes, and selected native
   interaction contract/build hashes (or `null` if absent). No host paths/PIDs
   enter the key. Native build/bundle manifests remain distinct identities.
   `setup_dependencies` binds semantic content of actual family-declared setup
   references, by role, task ID, template and configured-content digest. Today
   only `mixed.SETUP_REFERENCES` composes other tasks: crafting C2 and encounter
   B1. Both actual setup and qualification use `suite.setup_dependencies` and
   that same declaration, not separate admission lists. Its direct composition
   calls primitive crafting/combat setup; nested compositions are not supported.
   Missing, ambiguous or wrong-family references fail before fixture actions.
   Configured definitions already contain the selected global placements and
   player equipment. Template defaults and other executable defaults are bound
   through the Python implementation digest.
   Raw `scenes.json` bytes are not part of the code digest. Raw
   `task_manifest_sha256` is reported outside the qualification key for provenance
   only. Formatting changes, unrelated task additions and unrelated content
   edits do not revoke unchanged task qualifications. Changing the actual task
   or an actually referenced setup dependency does.
   CLI `run --variant` changes configured content; derive the corresponding key
   using the configured definition and selected `definitions` mapping through
   `support.resolve` (or an author
   manifest with `selected_variant`), not another variant's key.
2. Later authorized renderer integration supplies actual operation/effect,
   scorer, visibility, negative-boundary and cleanup evidence for the claimed
   surface. Keep its artifacts and hashes. A curator must review authenticity,
   exact identities and scope; the validator does not prove gameplay or fetch
   artifacts. No model win, new threshold/quota, or mandatory live service is
   required by this data workflow.
3. A curated record has exactly these fields:

   * `key`: the exact complete key above; no wildcards or task aliases.
   * `status`: `engineering-qualified`, `partial`, or `unqualified`.
   * `scope`: a nonempty description of what the engineering evidence proves.
   * `evidence`: a nonempty list of objects with exactly `reference`, `sha256`
     (lowercase SHA-256), and `scope`. Use portable archive references, not
     private host paths; the hash identifies the referenced evidence bytes.
   * `allowed_run_modes`: an explicit nonempty subset of `setup`,
     `interface-smoke`, `task-engineering`, `mechanism`, `capability` for an
     `engineering-qualified` record; empty for partial/unqualified records.

   The resolver still enforces native features, terrain, mode/profile and
   family/mechanism restrictions. The recovery template's image surface
   (including T05 and data aliases) remains **task-engineering only** even if
   a positive record lists capability mode. All support cells retain
   `accepted=false` and `model_success=not-inferred`; engineering qualification
   does not accept a release or a model result.
4. Validate proposed data locally before including it in a new package:

   ```sh
   rpg-benchmark validate-qualification --records /absolute/proposed-records.json
   ```

   The check is strict about fields, hashes, helper/source consistency,
   evidence presence and duplicate/conflicting keys, including unused records.
   A missing or nonmatching record cannot promote a cell. Partial/unqualified
   evidence is not positive evidence. Validation alone does not install records.
   After curator approval, update the packaged data, rebuild/reinstall through
   the existing package workflow, and create a NEW binding with the updated
   package inventory. The existing runtime content check prevents unnoticed
   package/data substitution. No `run.py`, renderer, player or adapter task list
   edits are needed to promote an arbitrary-ID task. This current record format
   is renderer-only; it does not certify public headless delivery.

Without `--config`, `support` labels its basis **source-only**, leaves native
identity unset and cannot certify whatever binary a caller later chooses.
Unqualified renderer execution still requires `--unqualified-engineering` in
an allowed engineering mode; that opt-in never opens capability admission.

### A genuinely new mechanic

Add one family module and register it once in `suite.MODULES`. The small existing
contract is:

* `setup(admin, actor, definition, terrain)` establishes and validates the real
  native fixture; `score(fixture, final, receipts, events, samples)` consumes
  actual native facts. Reuse existing setup helpers and private scorers.
* Declare `LEGACY_TEMPLATES = {}` and `TEMPLATES` with a semantic name,
  `requires`, and any `required` data fields; declare new data field names in
  `PARAMETERS`. Unknown templates, parameters and feature names fail explicitly.
  Parameter-specific checks belong in the family's `validate_definition`, when
  needed (for example combat's exact body/asset/supply shapes), not core ID cases.
  Do not add speculative runtime hooks or a task DSL.
* Ordinary families need nothing else. Runtime options describe only existing
  responsibilities: deferred actor startup, actual small-world/resident fixture
  needs, or retained private owner. Existing eventful navigation also owns
  `update_event`, `public_observer` and environment finalization. Add lifecycle
  behavior only for a demonstrated mechanic, not a six-hook framework.
* Legacy families adapt concrete internal case selectors at their semantic
  boundary; `task_id`, template and task-content digest retain author identity.
  Do not replace a scorer, simplify a goal or present a complete task solver.
* A genuinely new native feature needs its owning native/UI implementation,
  explicit feature declaration, negative fence/effect tests, and later exact
  native/profile integration evidence. Registration alone is not feature support.

  The legacy headless admission policy is retained. Additional families being
  unqualified there is not evidence that shared-client skill/trade/dialogue
  operations are absent. The public build/bundle path remains renderer-only.

Generate docs with `python -B tools/generate_task_docs.py --write`, then `--check`.
After deliberate native edits, `python -B tools/refresh_native.py --write` records
the successor source/contract selection while retaining historical baseline pins.
Run `python -B tools/package_source.py --write` and `--check` after the final
source batch. Package/install away from this checkout as in TESTING.md.
Keep MIT harness code separate from GPL native derivatives and external asset
licenses. Linux/aarch64 is the selected scope; no new platform or asset rights
are implied. No commit, tag, publication or model capability is implied by a
successful engineering check.

Issues and small pull requests are welcome. State the observed problem,
reproduction command, environment and expected behavior. Never include
credentials, personal paths, private oracle data, assets or large run logs
in an issue or commit.

For code changes, run the relevant tests from [TESTING.md](TESTING.md) and
report exactly what was and was not checked. Keep packaging fixes separate
from task/scorer changes. Do not weaken predicates, hide invalid attempts or
expand support claims to obtain passing tests. Discuss semantic changes to
task goals, seeds, fixtures, evaluator or admission before treating them as
comparable benchmark results.

New adapters must respect public/private boundaries, strict identity and
receipt accounting and explicit provider/model configuration. Do not add a
whole-task solution to the evaluator.

Preserve upstream attribution and license boundaries. Harness contributions
are MIT; native derivative contributions remain under the native component's
GPL-3.0-or-later terms. Assets require separate review and are not accepted
as part of the initial source-only scope.
