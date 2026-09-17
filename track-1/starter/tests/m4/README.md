# M4 implementation record

Module: M4; interface: `rca-v1`. This module replaces the example routed agent with
one bounded controller. `solve(instruction, dataset_dir, ctx)` returns the official
`Solution`; `investigate(case, state, deadline=...)` returns a `Decision`, a prebuilt
fallback, and the full merged evidence ledger. Public development labels are never
read by these runtime modules or tests.

For example, the synthetic fixture in `helpers.py` supplies a CPU anomaly for
`synthetic-pod`. Deterministic investigation returns one answer for that observed
component, a compatible reason hypothesis, its sampled onset, low confidence, and
an empty per-model usage dictionary. These fixture records are synthetic and are
not presented as real experiment evidence.

Actual checks on Windows, Python 3.12.14 bundled runtime:

```
python -m unittest discover -s tests/m4 -p 'test_*.py' -v
# 38 offline tests pass; optional real-window check skips without RCA_TEST_DATA.

RCA_TEST_DATA=<official Market-cloudbed-1 directory>
python -m unittest discover -s tests/m4 -p test_real_window.py -v
# Second query.csv case, actual M1/M2/M3: pass.
# Investigation 36.718 seconds; 1156 evidence records; 1 answer; 0 model requests.
```

The real-window number is a local Windows measurement, not a 2 CPU / 8 GB Docker
claim. It uses label-free `query.csv` and telemetry only. No paid calls were made.

Coverage includes provider HTTP-200 error bodies, empty choices/content, invalid
JSON/IDs/queries, missing usage, bounded attempts, run/case cost reservation,
deadline exhaustion, pinned-model fallback prohibition, shared circuit breakers,
per-case usage deltas, deterministic operation without a client, reference
collisions, distinct IDs for fused reason hypotheses, correlated metric fusion,
multiple episodes, independent trace
triage, renderer invalid/failure, and parse-failure best guesses.

Unknown provider token counts remain null in `diagnostics/routes.jsonl`; their
reservations stay charged to the internal budget. Official usage totals contain
observed token counts plus `unknown_usage_calls`, so offline evaluation must not
mistake incomplete provider accounting for a measured zero cost.

`diagnostics/evidence/<invocation_index>.json` atomically persists the full case,
final decision, structured evidence and observed catalog, with aware ISO datetime
strings. This supports independent replay without dumping all measurements into
the Markdown explanation. It contains no raw model prompts or credentials.
Unexpected provider model identities retain their actual attribution and an
unknown cost; they never inherit the requested model's price.

Missing/failed tools return explicit coverage warnings. No supported component
causes a disclosed `unknown-component` placeholder; absent onset uses a disclosed
window-start guess. A fully unparseable window uses an explicitly unverified epoch
placeholder when the time field is requested. Rendering retries the prebuilt
fallback once, then emits four deterministic emergency sections. All actual
calls remain accounted even if the model decision or rendering is rejected.

Limitations: ranking thresholds are engineering defaults, not calibrated on a
held-out case set. The conservative deterministic gate requires direct mechanism
support and is generally closed for ambiguous resource/network hypotheses.
Network subtype remains unresolved without mechanism-specific evidence. No live
GLM accuracy, routing savings, final Docker resource limit, or full development
denominator claim is established by these tests. Full solve integration requires
the M5 renderer and root integration checks.
