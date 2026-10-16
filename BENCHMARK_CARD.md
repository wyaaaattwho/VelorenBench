# VelorenBench benchmark card

## Purpose and release

Evaluate external agents' decisions and tool use in native RPG tasks:
resource planning, navigation, crafting, combat, support and NPC contracts.
This is initial source release 1.0.0, based on local freeze 1.0.0/build001.
It is not a benchmark of general intelligence or a calibrated difficulty
scale, and it publishes no model campaign, baseline scores or leaderboard.

## Unit of evaluation

An episode is identified by task definition/fixture version, terrain, seed,
backend, native/source/scorer build, modality, action profile, adapter/model
configuration and optional T06 weapon condition. Catalog size is 29
definitions, 17 originals plus 12 additions, and 37 raw terrain combinations.
Use the admitted [support matrix](SUPPORT.md), not a universal catalog
denominator. The current manifest seed is 130626853.

Fixture construction may use private native setup controls and authored
events; the player is a non-admin actor. For example, T02's route closure is
benchmark-authored collision, not a claim about spontaneous world collapse.
T08's finite archer injury and T09's initial combo are disclosed conditions.
T06 builds are not claimed equal in difficulty.

## Observation and action boundary

Agents receive a public macro goal and ordinary structured state, verified
image bytes or both, as admitted. Private oracle/setup/judge state is not
player input. Image-only tasks do not receive a hidden structured fallback.
Tools perform native interaction and timing, not a fixed whole-task solver.
Actor/session references, action ownership and strict integer transport are
validated. Renderer and inherited headless environments are not timing- or
world-equivalent.

## Scoring and validity

Oracle success checks actual inventory/equipment/effect/location/trade/
contract predicates appropriate to each task. T12, for example, requires
normal acceptance, actual own damaging contribution, death of the contracted
target, living return/native resolution and exact reward insertion in the
same settlement chain. The public source export does not relax predicates.

Native goal satisfaction, model-attributed evidence and measurement validity
are distinct. Entry errors, unknown effects, missing receipts, transport
errors, timeout/interruption and incomplete cleanup must be retained and
reported independently, not converted to successes or erased from attempts.
An engineering or setup pass is never a model capability result.

## Reporting checklist

- Exact source commit/export identity, package/native/scorer hashes and
  fixture versions; task/terrain/modality/action profile/build.
- Planned and attempted cells, number of seeds/repeats, all-attempt and
  valid-episode denominators, aggregation and retry/exclusion policy.
- Declared model/provider/effort, adapter source and configuration; externally
  observed runtime model metadata separately (`unknown` when unavailable).
- Seeds, world/setup configuration, agent/tool budget, evaluator horizon
  (including unset), stopping criteria and error policy.
- Goal-success numerator, independent validity counts, setup refusals,
  invalid episodes, transport/unknown effects, interruptions and cleanup
  failures. Avoid comparing incompatible denominators/modalities.
- Immutable original results plus clearly labelled derived evaluations,
  timing assumptions and uncertainty; audited artifacts where shareable.

## Evidence and limitations

Local qualification consists of bounded native engineering witnesses and
unit/contract/installed-artifact checks, not autonomous model performance.
T12 hybrid has historical native success. Structured qualification includes
corrected replay of preserved native evidence; it is not a fresh native win
and does not rewrite the original false/interrupted/unknown result.
The chronological stop check uses conservative same-host time mapping; it
does not establish authoritative client/server tick alignment.

Four pre-entry refusals occurred in nine heterogeneous local setup attempts.
That observation remains a setup reliability limit, not a population rate,
a model failure rate or proof of an exclusive cause. Raw runs are excluded
from this repository; these are scoped inherited qualification statements,
not independently reproduced public results.

Selected native deployment is Linux/aarch64 with system libraries, GPU/Vulkan
and Xvfb dependencies and external mixed-license assets. A clean-machine
build, all-platform/GPU compatibility and full asset-availability closure
have not been established. Native variable-dt clocks and frame/effect
ordering introduce uncertainty. Image-only whole-task coverage remains
limited to the stated cells. Source/package verification does not close
these limits or establish deterministic gameplay.

## Intended use and maintenance

Use for controlled, explicitly reported agent research and interface
development. Do not present setup or synthetic checks as model scores.
Contributions should preserve original evidence and distinguish changes to
packaging from changes to goals, scorer semantics, fixtures or admission.
See [NOTICE.md](NOTICE.md) for license boundaries and asset responsibilities.
