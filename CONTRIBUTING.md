# Contributing

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
