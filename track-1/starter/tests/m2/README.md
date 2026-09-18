# M2 delivery and checks

Implements `agents.rca.metrics` and `agents.rca.onset` against **rca-v1**. Ownership was assigned explicitly by the user through the coordinating agent. No shared types, runner, labels, dependencies, or final ranking changed.

Caller example (run from `track-1/starter`):

```python
import time
from pathlib import Path
from agents.rca.runtime import parse_case
from agents.rca.data_access import CSVTelemetryStore
from agents.rca.metrics import triage_metrics, replay_evidence

case = parse_case(instruction)
store = CSVTelemetryStore(Path(dataset_dir))
bundle = triage_metrics(case, store, deadline=time.monotonic() + 40)
record = next(record for record in bundle.evidence if record.transform == 'metrics.baseline_compare.v2')
assert replay_evidence(record, store, deadline=time.monotonic() + 40) == record.values
```

The other public functions are `inspect_metrics(case, component_ids, metric_families, store, *, deadline)` and `compare_replicas_and_node(case, component, store, *, deadline)`. All return `AnalysisBundle(module='m2')`. M4 consumes candidates and M5 consumes/replays evidence. The `metrics.*` features describe measured strength, family, persistence, sample cadence, reference sensitivity, and observed comparisons. Strength is bounded to 20 and is not a probability. Related KPI evidence shares a component/family correlation group. Followup candidates get distinct stable IDs.

## Checks executed

- `python -m unittest discover -s tests/m2 -p 'test_*.py' -v`: 19 synthetic tests passed; real check explicitly skipped when `RCA_TEST_DATA` is unset (20 discovered). Uses Python 3 and bundled pandas/numpy on Windows.
- `RCA_TEST_DATA=<official bundle> RCA_TEST_ROWS=0,7 python -m unittest discover -s tests/m2 -p 'test_metrics.py' -v`: real checks on both label-free query windows and exact representative evidence replay passed. This invocation before the final missing-node regression had 11 tests and took 19.137 seconds. This is local wall time, not the 2 CPU / 8 GB acceptance result.
- Real row 0 scan retained `shippingservice-1` read activity despite service aggregation. `container_fs_reads./dev/vda`: 10 reference samples, median 0, peak 48608 native units; last normal 09:08, first sustained change 09:09, peak 09:10, all UTC+8. `container_fs_reads_MB./dev/vda` peak was 15138.8828125 native units. These measurements were generated from the official telemetry; they are not detector constants or scoring labels. Locator examples include records 934719 and 1400352 in `telemetry/2022_03_20/metric/metric_container.csv`.
- Synthetic cases cover stable-zero changes, counter reset behavior when semantics are explicitly known, unknown semantics without differencing, missing baseline, non-finite samples, duplicate timestamps, reference sensitivity, gaps, single spikes, multiple episodes, unseen component names, service-flat/pod-anomalous behavior, node cochange, missing node data, all three evidence transform replays, row caps and deadlines.
- No model/API calls and no paid runs. No development labels were read by these tools or tests.

## Failure behavior and limits

Missing, empty, partial, and deadline coverage is returned explicitly; none means healthy. Unknown topology is not guessed. Every transform records the exact retained input-prefix length per query. Replay uses that same prefix, including partial and row-capped input, and raises when the original prefix cannot be recovered. Missing reference data yields descriptive evidence without an unsupported anomaly onset. Node cochange is `None` when node measurements cannot establish it.

Scans retain at most 250,000 matched window rows/source and explicitly report partial coverage at that safety limit. All analysed series are retained; the former 512-series cutoff has been removed. Sources borrow unused module time while reserving a small amount for subsequent sources; the module deadline still applies. Comparisons include at most 24 observed components. Many resource candidates are expected; M4 owns cross-source ranking, the explicit model shortlist, and exact fault count.

Official KPI gauge/counter semantics are unconfirmed, so the runtime preserves raw values and `native/unknown` units. Counter reset handling exists for explicitly verified semantics; the default verification registry is empty. Resource mechanism labels remain hypotheses. Network hints cannot identify a legal network subtype. Service names may not map to pod service names, in which case no service clue is attached. Onset estimates are midpoints of observed intervals, with sampling gaps/precision disclosed. These two windows do not measure general accuracy, candidate recall, or hidden-test performance.
