# Testing and package verification

## Source release v1.0.1

Install the current wheel before process tests so `rpg-episode-bridge` exists
in the test interpreter's `bin` directory. `test_process_contract.py` covers
the actual installed console process, capability-based admission, rejection
before world allocation, real byte serialization/wire accounting, and observed
non-success versus unknown final state. Existing lifecycle fixtures implement
the added preflight seam without changing their outcome/error assertions.
Those process/media peers are synthetic engineering fixtures, not native or
real-model qualification. Historical-fixture skips already present in this
export are not qualification passes.

The actual official dsh integration must additionally be checked in its own
declared environment through its Cordis/plugin/attachment/pi-ai loop; a mock
Python Agent or a support-matrix row cannot substitute for that check.
Retained official-DSH native/model evidence and the bounded integrated public
core/DSH review are described in [PROVENANCE.md](PROVENANCE.md). E246 subsequently
exercised final008/core1.0.1 in six selected native/model attempts; its four
valid and two invalid Agent-side measurements are not a suite-wide
qualification or ability estimate. Publication preparation does not repeat
native/model runs for documentation/status-only changes.
GameAgent is an external adapter, not distributed or fully qualified; its internal
checks are not a benchmark release gate. Synthetic tests alone do not qualify
any adapter's real model/image transport.

Retained engineering receipts report core007 **882 passes, 120 historical
skips and 105 passing subtests**, public DSH **34 contracts**, and final008
fresh wheel/sdist checks **88 passes and one historical skip each**. These
are prior receipts, not new full-suite runs of the publication payload.
Executable core/adapter payload, task/native/data and test bytes are unchanged
from final008; final packaging uses fresh local wheel/sdist paths and isolated
installation/source/entrypoint checks. Receipt labels and selectors are in
[PROVENANCE.md](PROVENANCE.md); validation artifacts are not release downloads.

Use Python >=3.13 and keep environments/build outputs outside the checkout.
The core runtime needs no third-party packages; pytest/build dependencies
are separate.

```sh
python3.13 -m venv --copies /absolute/test-env
/absolute/test-env/bin/python -m pip install --require-hashes \
  -r requirements-build.lock -r requirements-test.lock
/absolute/test-env/bin/python -m pip install --no-deps --no-build-isolation .
/absolute/test-env/bin/python -B -m pytest -q -p no:cacheprovider \
  --basetemp /absolute/test-tmp \
  harness/tests/test_candidate054.py harness/tests/test_portability.py \
  harness/tests/test_public_source.py
```

These focused tests do not need a provider, native binary, game asset or
account. `test_candidate054.py` retains historical build-flag checks as well
as exact task/scorer byte identities; it does not override the current
release's provenance.

## Optional SDK and full public suite

The original SDK tests import the optional Copilot package. To collect the
whole suite, install `requirements-test-sdk.lock` as well, then run
`python -B -m pytest -q -rs -p no:cacheprovider --basetemp /absolute/full-test-tmp harness/tests`.
The SDK's installed binary/CLI must not be confused with a live authenticated
service; these tests use mocks, pipes and loopback, not provider inference.
Without the optional SDK, select the SDK-free files above rather than
claiming a full-suite pass. The source export does not silently ignore SDK
test collection errors.

## Historical regressions (explicitly optional data)

Four immutable historical fixtures are **not distributed**:
`accepted-contracts.json`, `native-contract-traces.json`,
`scorer-identity-052.json`, and `stability-repair.json`. They contain archived
source ASTs and run-derived native data outside this source-release scope.
The tests that require them call `tests/historical.py`. Without the explicit
environment variable below, those cases report a visible **skip**, not a
pass. No synthetic replacement is counted as the original regression.

For an authorized holder of those exact fixtures:

```sh
VELORENBENCH_HISTORICAL_FIXTURES=/absolute/authorized-fixtures \
  /absolute/test-env/bin/python -B -m pytest -q -rs -p no:cacheprovider \
  --basetemp /absolute/historical-test-tmp harness/tests
```

The helper verifies each original SHA-256 before use. Selecting missing or
altered data fails; it never skips. The original semantic assertions,
counterfactuals, expected outcomes and seeds are unchanged. Keep fixtures
outside the checkout and distributions. A public-source suite pass must
state the skipped historical cases and cannot claim the full historical
regression suite passed.

## Wheel and sdist installation away from the checkout

Build as in BUILD.md. The verification helper creates a new venv/HOME,
installs an artifact, proves imports come from it and the core initially
has no SDK/runtime dependencies, runs CLI/help commands, then installs
test dependencies and copies only source tests/tools into an isolated work
directory. It logs commands and receipts **outside** the publishable tree.

```sh
python3 tools/verify_install.py --artifact /absolute/python-dist/FILE.whl \
  --root /absolute/new-wheel-check \
  --tests test_candidate054.py test_portability.py test_public_source.py
python3 tools/verify_install.py --artifact /absolute/python-dist/FILE.tar.gz \
  --root /absolute/new-sdist-check --sdk
```

Use `--sdk` for the full original test collection. For offline installation,
pass `--wheelhouse DIR` containing all hash-locked required build/test wheels;
otherwise pip uses its configured public index. No internal wheelhouse is
required. Native source/tools identity is verified separately from the Git
checkout using `python3 -B tools/package_source.py --check` and
`build_native.py --check` with external assets. These checks do not compile
Rust, validate a GPU or create gameplay/model results.
