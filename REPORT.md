# Track 1: bounded, evidence-backed RCA agent

The five-module agent is implemented in the official `track-1/starter/` runtime. It reads only whitelisted telemetry, builds replayable metric/trace/log observations, ranks bounded hypotheses, optionally asks permitted GLM models to select among those hypotheses, and deterministically renders the requested answer fields and four evidence sections. The available real measurements show a working integration with limited diagnostic accuracy. A paid routing-versus-single-model result and Docker acceptance remain unmeasured.

## Actual development results

| Experiment | Planned / returned | Official mean partial | Fully solved | External elapsed | Actual model requests |
|---|---:|---:|---:|---:|---:|
| Deterministic integration, original IDs 0, 1, 4, 5, 6, 8, 9, 25 | 8 / 8 | 0.1775 | 0 / 8 | 245.609 s | 0 |
| Native resource check, original IDs 0, 1 | 2 / 2 | 0.0 | 0 / 2 | 59.765 s | 0 |

The eight-case run used code revision `3172a081366080512261771e38ab96e3feaab6ef`. Cases were chosen as the first occurrence of each task type plus a multiple-failure case, using label-free instructions rather than development answers. This is a small public-development integration sample, not a 70-case development benchmark, independent holdout, or hidden-test result. The official evaluator was not changed. One repetition was run, so variance is unmeasured. Per-case elapsed time averaged 30.6275 seconds, median 30.975, maximum/p95 31.77; the external timer also includes startup and saving. Eight bypass events and zero actual HTTP requests establish zero runtime model cost for this deterministic run. This does not establish routing savings or model quality.

All eight original row IDs were present exactly once. There were no unexpected IDs, missing evidence files, invalid four-section layouts, or denominator exclusions. One output passed the structural validator as valid and seven were degraded because telemetry/coverage or ordering remained uncertain. Structural validity is not diagnostic correctness. The partial scores were 0.75 for original row 9, 0.67 for row 25, and zero for the other six cases. The official evaluator rounds per-case partial scores; the reported mean preserves those official values.

The two-case native resource check used code revision `9acaa72` and observed the actual Python interpreter pinned to two logical CPUs. Peak observed RSS was 261,902,336 bytes (249.77 MiB), sampled every 200 ms; an 8 GiB process-memory guard did not terminate the run. External time was 59.765 seconds. This is a native Windows measurement, not a Docker/cgroup guarantee. The earlier eight-case monitor observed a launcher rather than its interpreter child; its peak RSS and enforced CPU limits are therefore explicitly unverified and are not reported as resource acceptance.

Artifacts with actual run manifests/source hashes, generated predictions, audited case counts, and resource observations are in [`eval/results`](eval/results). Raw telemetry and full structured ledgers are deliberately excluded. Later correctness fixes to audit/replay/rendering do not retroactively change the frozen runtime source revision of either experiment.

## Evidence audit

The saved structured ledger for original row 0 of the eight-case run contains 1,704 unique records. The audit verified that its four referenced telemetry files exist and that decision/alternative references have no dangling evidence IDs. Using a disclosed deterministic sample—first evidence ID in lexicographic order within each transform—it replayed six records: metric baseline, metric replica/node comparison, service summary, trace group, trace pairing quality, and network start-gap comparison. All six numeric replays matched, and all 34 sampled source locators were found. This is six sampled aggregate checks, not a claim that all 1,704 aggregates were replayed. Physical units remain `native/unknown` when undocumented. Causal support was not independently reviewed by a human.

The metric module independently recovered the documented row-0 read-activity change from telemetry: a last normal sample at 09:08, first sustained change at 09:09, and peak at 09:10 UTC+8. That observation did not make the final deterministic diagnosis correct. It illustrates the remaining gap between candidate recall and causal ranking. Component names, values and example times are not detection constants.

Each evidence record stores query definitions, source paths, original record locators, transform version/parameters, measured values, units, coverage and limitations. Partial scans record their retained input prefix, so replay uses the same records. The human evidence display contains every referenced ID, bounded supplementary observations and explicit omission counts; full structured JSON remains under the run output's `diagnostics/evidence/`. Re-rendering the real row-0 ledger with the compact display produced 41,825 UTF-8 bytes and 19 displayed evidence records, with unchanged structural validity.

## Method and failure behavior

- **M1:** shared UTC+8 contracts, instruction parsing, incremental component catalog, bounded provenance-preserving CSV scans, runtime state, official runner integration and packaging.
- **M2:** container/node coverage independent of service shortlist, robust and zero-MAD comparisons, sampled onset intervals, short spikes, multiple episodes, replica/node comparisons and replay. Gauge/counter semantics are not guessed; the verified-counter registry is empty until semantics are confirmed.
- **M3:** independent span-group anomalies, duplicate/parent-aware pairing, safe start-gap features, targeted literal log patterns and replay. Trace duration remains in native units and dependency direction is not causal proof.
- **M4:** bounded investigation, correlated-evidence deduplication, episode selection, deterministic best guess, allowed-GLM routing, cross-case circuit breakers, bounded retries/fallback, per-attempt routes and observed usage. Single-model mode forbids cross-model fallback. Missing provider usage remains unknown and keeps a conservative budget reservation.
- **M5:** stateless requested-field/count/layer/reference validation, deterministic four-section evidence, exact original-row denominator audit, isolated experiment manifests, same-agent comparisons and source/replay checks.

Missing, empty, partial, failed and unqueried coverage never means healthy. Every case retains a best guess; doubts belong in evidence. The validator distinguishes malformed/contradicted answers from unknown catalog coverage. Development labels are used only by the offline evaluator. Real source data is never used as an answer lookup table.

The measured diagnostic failures remain substantial. Generic resource changes can outrank the causal component; correlated CPU, memory and I/O changes are difficult to separate. Weak network hints cannot establish packet-loss/corruption/retransmission subtypes. Missing reference samples, sparse traces, partial scans and ambiguous episode separation reduce reliability. The current bounded metric evidence cap can reduce candidate recall and explicitly reports that limitation. The report does not infer a general accuracy increase from two windows or compare this eight-case result with the official heuristic's different 70-case denominator.

## Evaluation and remaining acceptance

The offline harness freezes original IDs/order, source hashes, resolved runtime budgets, price table, data identity, cache conditions, repetition and resource conditions before execution. It creates separate directories and strips scoring fields from runner input. `audit_run` uses the unchanged official matching, keeps missing cases in the planned denominator, marks duplicate/unexpected predictions invalid, counts every usage attempt, and separates external elapsed time from summed case time. It never converts absent token measurements to zero dollars. `compare_runs` rejects unsupported savings claims when conditions differ, pricing is incomplete, or either routed/single-model side made no actual calls.

M5's 28 synthetic tests cover output field combinations, invalid/degraded rules, finite evidence, immutable rendering, missing/duplicate/unexpected cases, noncontinuous IDs, repeated usage, missing provider tokens, unpriced models, resume mappings, dry-run label stripping, comparison fairness and source replay. CLI help and a two-repetition real-query dry-run were executed with no model calls and no experiment directory created. The coordinating integration tests exercise the modules together and provider failures through local stubs; these are not real-provider quality measurements.

The subsequently recorded complete integration suite **passed 123 tests in 72.002 seconds, including five real-data checks and no skipped tests**. Its real trace check explicitly returned partial coverage under its 30-second budget; a passing bounded-runtime test does not make that scan complete. The official submission validator also completed **two real cases with zero warnings**, confirming the required predictions/evidence shape. Those validator results do not establish diagnostic correctness or replace the development accuracy results above. Actual OpenAI-compatible SDK transport tests against localhost verified HTTP-200 error fallback and disabled hidden SDK retries; they made no external paid API calls. Sanitized transcripts are saved in [`eval/results/verification`](eval/results/verification).

These counts describe that specific completed verification run. A later disclosure-only bypass regression was being added separately and is not included in the 123-test result; this report does not pre-claim its execution.

Still unmeasured:

- Same-agent single-GLM versus routed accuracy, actual paid cost, latency and repeat variance: no Featherless key was available for this session.
- Docker build/run under enforced 2 CPU / 8 GB container constraints: Docker was unavailable. Native measurements are reported separately.
- Full 70-case development accuracy, held-out deployment accuracy, calibrated confidence and causal explanation review.

Reproduce the comparison plan with `python eval/run_comparison.py --help` and the dry-run command in [`eval/README.md`](eval/README.md). Actual model runs require the supplied Featherless endpoint/key and a new output directory. No claim of measured routing savings is made here.

## AI and source disclosure

Implementation and tests were generated and reviewed with OpenAI Codex using a GPT-6-family coding assistant and delegated subagents in isolated Git worktrees. No additional agent framework was introduced. The human provided the project objective, module boundaries and repository constraints; Codex performed the implementation, debugging, test execution and report assembly. Exact Codex model variant and per-assistant token/cost accounting were not exposed in the saved experiment manifests, so they are not claimed as measured usage.

The official starter supplied the runner interface, output formatter, model price table, heuristic baseline and unchanged OpenRCA evaluator. The new modules, routing logic, deterministic evidence validation and offline audit harness are team additions. Runtime model options are the allowed Featherless GLM family documented in Track 1; none were called in the reported deterministic experiments. Data attribution and licensing remain in the official repository's attribution files. AI-assistant development activity is distinct from the runtime usage ledger and is not counted as Featherless inference spending.
