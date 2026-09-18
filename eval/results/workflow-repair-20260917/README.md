# Workflow repair checkpoint — 2026-09-17

This is one fixed 20-case **public-development** run, not a hidden test, a full 70-case result, or a matched routing comparison. The exact original IDs, runtime source hashes and local Docker image ID are in `manifest.json`. The official accuracy evaluator is unchanged. All 20 predictions and four-section evidence files were emitted without duplicate or missing IDs; compact predictions are included here, not raw telemetry or full evidence ledgers.

| Measurement | Result |
|---|---:|
| Official mean partial | 0.346 |
| Fully solved | 5 / 20 (25%) |
| Complete planned workflows | 19 / 20 |
| Final model selections retained | 20 / 20 |
| Container wall time | 608.960 seconds |
| Mean / maximum case time | 30.422 / 47.630 seconds |
| Python child peak RSS | 494,301,184 bytes |
| Container cgroup memory peak | 2,195,165,184 bytes |
| Valid / timed-out / empty model responses | 36 / 3 / 2 |
| Observed token cost | $0.113872955 |
| Additional retained unknown-cost reservations | $0.0306708 |

Three requests timed out without provider usage. Their final charges are unknown; observed cost is not a complete bill. No response reported token-length truncation. Case 60 retained a valid Flash answer, but its planned GLM-5.1 upgrade timed out and left too little time for GLM-5.2. It is deliberately marked workflow-incomplete even though its answer scored correctly. All metrics/trace triages completed; three targeted container queries returned explicit empty results, not inferred health. All 20 actual followups used replica/node comparison. Log/dependency followup branches have synthetic controller tests and separate M3 real-tool verification; they were not exercised by these 20 real controller decisions.

The run used the default `python run.py --dataset /data --queries /out/queries.csv --out /out` entry point, no `--agent`, read-only data/root filesystem, 2 CPUs and 8 GiB enforced by Docker. The query subset contains only public instructions and original IDs. Process/window caches started empty; operating-system disk caches were not cleared. `resources.json` records the actual limits and external child timing. The runtime hashes still matched the implementation when audited.

The same first ten IDs scored mean partial 0.250 and 2/10 fully solved, versus the preceding agent review's 0.125 and 0/10. Tools, ranking, prompts and routing all changed; this is not isolated evidence of a routing benefit. Repetition variance and a new matched single-model comparison are unmeasured.

## Implemented corrections

- Reuse complete covering windows in a bounded cache, preserving raw IDs and record locators.
- Read trace baseline/incident/padding once; vectorize pairing and statistics; remove arbitrary trace-row/edge and 512-metric-series cutoffs. Memory/deadline safety limits remain explicit.
- Calibrate ranking strength, preserve coherent representative evidence, and distinguish weak mechanism guesses.
- Bound complete prompts, retain the minimum distinct fault set, and preserve located literal log excerpts.
- Enforce wall-clock HTTP deadlines, record final-body versus reasoning metadata, and keep unknown usage/cost reservations.
- Route targeted followups and real candidate competition; report workflow completion separately from output validity and accuracy.

## Verification

- Full offline discovery: 183 tests, 178 passed and 5 opt-in real checks skipped.
- Those five real-data checks were then explicitly run: 5 passed, no skips, 25.090 seconds of unittest time. M3 reported complete coverage and 272 edges; M4 ran actual modules with zero model calls.
- `evidence-replay.json`: six transforms sampled deterministically from the 4,920-record row-0 ledger; 6/6 numeric replays matched, 34/34 source locators found, four source files present, no dangling references or integrity errors. This does not verify every aggregate or establish causality.

Real-check command, from `track-1/starter`:

```bash
RCA_TEST_DATA=/path/to/Market-cloudbed-1 python -m unittest \
  tests.m1.test_real_store tests.m2.test_metrics.RealTelemetrySmoke \
  tests.m3.test_traces_logs.RealTelemetrySmoke tests.m4.test_real_window -v
```

Diagnostic accuracy remains limited, and the uncompleted Strong fallback is a known runtime defect at this checkpoint. This snapshot is not a claim that the entire workflow goal or final competition submission is complete.
