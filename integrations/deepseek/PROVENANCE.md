# Reference integration provenance

This is the untagged adapter **1.1.0** candidate for benchmark core **1.1.0**, not a
registry publication or a vendored official runtime. Original integration
source is MIT; third-party runtime/assets remain separate. Use the same exact
core wheel in evaluator and adapter environments. The dependency and no-model
installation requires `rpg-benchmark-harness==1.1.0`; version text alone is
insufficient, because preflight also checks actual player-package bytes.
Verify the candidate source manifest and your locally built artifact hashes,
not a same-named registry package. Wheels/sdists are local validation outputs,
not uploaded release assets.

The supplied RPC client's `dsh_agent.py` SHA-256 remains
`2f2a5933bb9ef9ebff31631fec2ff0891bc7d59dad832658e06bdb71323595be`.
Its cancellation/late-reply behavior is retained. The public successor adds
lossless observation text, schema-scoped decimal integer argument transport,
official attachment admission and strict evidence replay; it is not
byte-identical to the earlier private staging prototype. The binding helper
reuses the public core runtime, freeze, capability and Adapter contracts;
it does not fork the runner, evaluator, native protocol or task solver.

## Official dependency identity

Public distribution metadata was checked for:

- `@deepseek-ai/dsh@0.2.1-alpha.3`
- `@deepseek-ai/dsh-llm-pi-ai@0.2.1-alpha.3`
- `@deepseek-ai/dsh-tools@0.2.1-alpha.3`

The shipped lock's resolved URLs are public `registry.npmjs.org` tarballs
with integrity fields; its transitive pi-ai version is
`@earendil-works/pi-ai@1.2.0`. Only the root project name/version changed from
the supplied runtime lock. Use
`npm view PACKAGE@VERSION name version license dist.tarball dist.integrity --registry=https://registry.npmjs.org/ --json`
to inspect public metadata. Registry availability and metadata are
time-dependent; no guessed Git URL or runtime checkout is bundled.

The original Python `requirements.lock` was version-only and is retained
unchanged. `requirements-hashed.lock` adds public PyPI release-file hashes
for the same versions. Build requirements are separately hash locked.

## Verification boundary

Retained local evidence includes independent evaluator/adapter installation,
a fresh official npm installation from the shipped locks, installed plugin
imports and no-world/no-model preflight. Synthetic RPC, content and actual
official-loop transport tests cover cancellation, exact integers/signed zero,
image-only projection, attachment history and native receipt joins.
These checks are not native benchmark results.

Real official-DSH native/model checks cover C1/structured, C1/hybrid and
N1/image in the bounded lineage described in the root
[PROVENANCE.md](../../PROVENANCE.md). The public-successor hybrid craft check
requested no frame; the separate image normal non-win verifies PNG delivery
and history, not successful navigation. No private-state leak was observed in
that audited image session. Refusals and cancelled mutations remain visible;
cancellation is not evidence of zero interim physical effect. Actual provider
backend/model metadata remains `unknown` unless externally exposed.

Historical core 1.0.1 carries retained runtime evidence through core/native
byte equivalence to public007 (itself help-only relative to005). Adapter
execution bytes are unchanged from007 except the exact required core-version
literal in preflight. E246 then exercised final008 directly: DSH C1/structured
craft and T05/hybrid recovery were valid, the latter with no frames; N1/image
was a valid normal non-win with 11 recorded PNGs and 149 history image blocks.
All three had complete owned cleanup. E246 does not show successful mixed
visual/state consumption; historical frame-bearing hybrid evidence is distinct.
The root provenance records exact E246 summary selectors and evidence hashes.
Historical publication preparation was documentation/status/inventory-only relative to008,
with byte-identical executable adapter/core payload, tasks, native source and tests.
Bounded integrated review found no remaining public core/DSH defect in its
reviewed scope, not universal task/provider/platform qualification.

The 1.1.0 successor changes binding/entry live declaration validation to the
shared public caller-owned schema and updates the exact core dependency. Those
changes do not make historical native/provider evidence current qualification;
no-model installation/process receipts and exact source mappings are separate.

Local receipts, environments, bindings and raw runs stay outside the export.
They are not portable example bindings or a model-performance campaign.
No private Agent implementation, credentials, binaries, assets, node_modules,
wheels or caches are included in this source integration.
