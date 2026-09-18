# Repository instructions — Track 1 team implementation

## Read before editing

Read `TEAM.md`, `docs/INTERFACES.md`, `docs/INTEGRATION.md`, and the specification for the module you are implementing under `docs/modules/`. Read the official Track 1 data/model/scoring/submission documentation before changing runtime behavior. The five module specifications define work to implement; their existence is not evidence that the code is complete.

## Ownership and scope

- Use the module owner/claim table in `TEAM.md`. If no module has been assigned in the current request, inspect and ask which module to implement; do not implement all five by default.
- Runtime implementation stays in `track-1/starter/`; do not create a second root `run.py` or a parallel Agent framework.
- M1 owns shared types, data access, runtime state, dependencies, runner defaults, Docker and Makefile. M2 owns metrics/onset. M3 owns traces/network/logs. M4 alone owns the controller, ranking, prompts, routing and model wrapper. M5 owns evidence validation/rendering and offline evaluation.
- Put module tests in `track-1/starter/tests/m1` through `m5`; M1 owns `tests/integration` and common fixtures. Keep fixtures tiny, synthetic or telemetry-only, and explicitly distinguish synthetic test records from real evidence.
- Consumers must not silently change shared types. Propose a contract PR with affected modules and migration steps; update `docs/INTERFACES.md` with the implementation.
- Use separate branches/checkouts/worktrees for concurrent writers. Preserve unrelated edits. Scope staging to the task. No force-pushing shared branches.

## Data, evidence, and runtime

- Never commit keys, raw datasets, local environments, notebooks with secrets, or bulk caches. Read `FEATHERLESS_API_KEY` and honor `FEATHERLESS_BASE_URL`; never print credentials.
- Agent/data tools read whitelisted telemetry only, never development answers, and write generated files/cache only under `--out`. The runner may read its explicit `--queries` file, but passes only instruction to the Agent, never scoring fields. Prefer label-free `query.csv`; the official validator can supply a dev query file to the runner. Only the offline eval harness consumes public development labels for scoring.
- Use aware UTC+8 times; trace timestamps are milliseconds, metric/log timestamps are seconds. Preserve raw units until verified. Missing/partial coverage is not health.
- Output the requested field set and exact fault count via official formatting. Always retain a best guess; record doubts in evidence. Do not hard-code example components, row IDs or answers.
- Measured values and source references come from structured evidence, never model-authored facts. Dependency direction alone is not proof of causality.
- Use only allowed GLM models for runtime calls. Bound scans, retries, fallback and total budgets. Record actual per-model usage and all routing attempts; do not claim unmeasured savings.
- Keep the official accuracy evaluator unchanged. Include missing cases in the planned denominator. Use distinct experiment directories.

## Delivery

Follow `.github/PULL_REQUEST_TEMPLATE.md`. Include actual checks, a caller-visible input/output example, missing-data/failure behavior, limitations and integration changes. Mark incomplete work as incomplete rather than returning placeholder success. Do not trigger paid runs merely to check documentation or perform offline unit tests.

Final packaging is M1's work: one root Dockerfile, one runtime implementation under `track-1/starter/`, compatible `python run.py --dataset /data --queries /data/query.csv --out /out` inside the image, and a no-`--agent` smoke test. M5 records real experiments and actual AI/tool usage in the submission report/README.
