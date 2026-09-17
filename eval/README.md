# Offline evaluation

The runtime never imports this directory. Development labels are consumed only here by the unchanged `track-1/starter/score.py:evaluate`. Every planned original row ID contributes exactly once to the denominator; missing and duplicate predictions score zero, and unexpected IDs fail run integrity. Output-shape checks are reported separately from official accuracy.

From the repository root, preview a same-agent single-model/routed comparison without API calls or output files:

```bash
python eval/run_comparison.py --dataset track-1/data/Market-cloudbed-1 --queries track-1/data/Market-cloudbed-1/query.csv --out out/comparison-new --rows 0,1,4,5,6,8,9,25 --repetitions 2 --single-model zai-org/GLM-5.2 --dry-run
```

The plan includes exact original IDs/order, resolved M4 policy/budgets, source hashes, dataset manifest identity, fixed official prices, cache/resource conditions, invocation mapping, and separate directories for every configuration/repetition. It strips labels from runner input. Full telemetry hashes are not computed and this is recorded. Remove `--dry-run` only when a configured Featherless key and real model comparison are intended; add `--dev-queries track-1/data/Market-cloudbed-1/dev/query_dev.csv` for scoring after execution. A reused output directory is rejected. Do not claim model savings if either side made no actual model calls.

Audit an existing run:

```bash
python eval/audit_run.py --manifest out/example/manifest.json --out out/example/run --dev-queries track-1/data/Market-cloudbed-1/dev/query_dev.csv --report out/example/audit.json
```

`audit_run(manifest, output_dir, dev_queries)` and `compare_runs(reports)` return JSON-compatible dictionaries. Multiple usage records for the same case all count toward tokens, cost and time. Unknown/missing provider usage or unpriced model names produce unknown cost, not measured zero. Invocation mapping comes from `diagnostics/attempts/*.json` when present; conflicting mappings fail integrity. Otherwise the frozen manifest mapping is used. `execution.json` records external process elapsed time separately from per-case time. Repetitions and variance are explicit; a single repetition does not measure variance. Fairness compares the actual frozen source hashes, query order, budget, price, data identity, cache and hardware conditions.

Audit source-backed structured evidence:

```bash
python eval/audit_evidence.py --ledger out/example/run/diagnostics/evidence/1.json --dataset track-1/data/Market-cloudbed-1 --samples-per-transform 1 --seconds 120 --report out/example/evidence-audit.json
```

The deterministic sampling rule selects the first N lexicographically ordered evidence IDs per transform. File existence and dangling references are checked for the full ledger; source locators and numeric replay are checked for the sampled records via M1 queries and M2/M3 replay. Numeric tolerance is `rtol=1e-10, atol=1e-12`; booleans cannot substitute for measured counts. Declared units are preserved, but physical unit semantics and causal support require separate review. Deadline/execution failures are reported as unverified.

`results/` contains small summaries of actual local development runs, generated predictions, source/config manifests and explicit measurement limitations. It contains no raw telemetry, development label files, full caches, or credentials. Large structured case ledgers remain in the experiment output directory.
