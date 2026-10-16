# Source-release provenance

The current source snapshot is named VelorenBench, version 1.0.0. Its
benchmark semantics originate in the selected local 1.0.0/build001 freeze
accepted on 2026-10-16. Pre-acceptance flags inside retained qualification
metadata describe build time; they do not mean the local freeze is pending.
Researcher authorization for this public source export was granted
separately. The Git commit of this repository, once created, identifies the
public source snapshot; no historical commit is fabricated.

| Identity | SHA-256 |
| --- | --- |
| Frozen Harness source-manifest artifact (not this export) | `314a6d204e4a5b8953d03e374de2273d38b812bf97813ce78f0c885cbe4352b8` |
| Task definitions (`scenes.json`, unchanged) | `a3ebef6fe7120633c23da32cc7610bf65a95b05a079216a4202fde415d24c8be` |
| Slay evidence evaluator (`contract_evidence.py`, unchanged) | `0afe32bb7e966fbce79c4772d8b1e06bd77730d090fe1ead0759cd049e7d4c91` |
| Full selected native tree | `4c2cc4bb7cb7e6a94f963253866f09c2a0a6fde0df2d23ece9c99f78322944db` |
| Full selected native source manifest (`native/source.json`, unchanged) | `e8f6e3a62c1ba28b2daf1f0be83deb8aa831109811361997d569b943544eaa08` |

The native tree digest is SHA-256 of compact sorted JSON mapping relative
paths to file SHA-256 values, not a Git tree SHA. The full selected inventory
contains 1158 files. This checkout exports 1157 of them byte-for-byte,
excluding the game asset `assets/voxygen/audio/null.ogg`. Its expected hash
remains in the full inventory. `tools/build_native.py` stages that one
compile-time resource from explicitly supplied external assets and verifies
the complete selected source before compilation. The public native directory
therefore must **not** be described as having the full selected tree hash
until that resource is legally supplied and verified in a separate build.

`source-manifest.json` is a fresh public-checkout inventory, excluding itself
and generated artifacts. `python3 tools/package_source.py --check` verifies
it; `--write` deliberately regenerates it after source edits. It records the
exported native subset identity separately. Python wheel/sdist are new MIT
Harness artifacts and do not reuse frozen artifact hashes. They exclude the
native tree and its metadata; use the Git source checkout for native builds.

The original task definitions, Python evaluator/runtime code (apart from
deployment-only host-protection data), native interface contracts and
exported native source bytes are unchanged. Source export edits cover public
docs, Python metadata/manifests, local build/assembly and verification tools,
portable host configuration, and test access to non-distributed historical
fixtures. No seed, task goal, oracle, score, admission cell or reward is eased.

Excluded: assets, native binaries, compiler/dependency caches, runtime worlds,
raw historical runs/recordings, internal materials inventories, qualification
controllers, unrelated GameAgent runtime and four historical test fixtures
containing run-derived data or archived source ASTs. Tests depending on those
fixtures remain opt-in with exact expected identities; see [TESTING.md](TESTING.md).

Historical qualification is scoped in [SUPPORT.md](SUPPORT.md) and
[BENCHMARK_CARD.md](BENCHMARK_CARD.md), without publishing operational logs,
private paths or results as a model campaign.
