# M3 implementation handoff (rca-v1)

M3 owns `agents/rca/traces.py`, `network.py`, `logs.py` and these tests.
The shared contracts and CSV store are M1 dependencies; no shared types changed.
Only M4 calls the tools. Runtime calls no models and consumes no development labels.

```python
case = parse_case(instruction)  # M1, instruction only
bundle = triage_traces(case, store, deadline=time.monotonic() + 15)
# AnalysisBundle(module='m3', candidates=[...], evidence=[...], edges=[...])
detail = inspect_dependencies(case, ('observed-pod-id',), tuple(bundle.edges[:1]),
                              store, deadline=time.monotonic() + 10)
logs = search_fault_evidence(case, ('observed-pod-id',), ('timeout', 'reset'),
                            ('log_service',), store, deadline=time.monotonic() + 10)
values = replay_evidence(bundle.evidence[0], store, deadline=time.monotonic() + 20)
```

Trace triage is independent of metrics. It compares component/operation/type
groups; `duration` stays native and status codes remain raw with explicit unknown
counts. Dependencies match unique `(trace_id, span_id)` identities, exclude
ambiguous duplicate parents/children and report missing parents. Parent-child
start differences are seconds, not measured network delays or causal direction.
Candidates deliberately leave reason unset. Small samples and unknown network
subtype are limitations, not evidence that a component is healthy.

Reads retain at most 150,000 matching trace rows per query, with 30 seconds of
context and a reserved analysis allowance. Analysis caps at 512 groups and 256
edges. Caps and timeouts produce partial coverage or explicit analysis warnings.
`retained_rows_per_query` records the observed input prefixes, so replay of partial
evidence does not silently include additional matching rows. A replay that cannot
recover the recorded prefix fails explicitly. No self-time is derived by
subtracting child durations.

Log searches use case-insensitive literal substrings; regex-like input stays
literal. They retain at most 50,000 rows/source and eight positioned matching
samples, including neighboring same-component records. Original text is preserved
up to an explicit 2,000-character limit. Empty, missing, not-queried and partial
are distinct. Compact results for eight completed signatures per store avoid
repeating identical searches during an immutable dataset run.

## Actual checks

Windows, bundled Python, `python -m unittest discover -s tests/m3 -p 'test_*.py'`:

- 12 synthetic tests pass; they cover same span IDs in different traces,
  duplicate parents, negative/asynchronous gaps, interval frequency normalization,
  native duration, raw/unknown status, low samples, missing sources, deadlines,
  padding, capped-prefix replay, multiline CSV logs, literal matching and cache.
- With `RCA_TEST_DATA` set to the official `Market-cloudbed-1` bundle, all 13 tests
  pass. The additional test reads the first instruction from label-free
  `query.csv`; it does not read development answers.
- With `RCA_TEST_SECONDS=15`, the real trace tool took 12.546 seconds and produced
  401 evidence records, three weak candidates and 256 edges. Both query coverages
  were partial and the edge cap was disclosed. The complete suite took 12.845 s.
- A 30-second budget run took 27.224 s for the suite and produced 401 evidence,
  four weak candidates and 256 edges, again with partial scans.
- All generated trace/network/log evidence in synthetic tests replays to exactly
  the original values and all trace positioning samples resolve to their source
  records. This is not a claim of real-data accuracy or a Docker resource test.

Additional bounded manual checks against that first label-free case passed:

- A real partial network record replayed exactly from input prefixes of 12,812
  baseline and 40,217 incident records (10.093 s).
- A directed real service-log search and replay completed in 11.704 s. Its
  `cartservice-2` record had 7,816 searched rows and zero keyword matches, with
  partial coverage explicitly retained. It made no health claim.
- The returned sample at CSV data-record index 1,048,730 was independently
  located with a CSV reader, confirming the original component and log ID.

Zero model/API calls. These additional real checks do not replace the automated
real smoke or establish root-cause accuracy. No mesh KPI expansion is included:
available dependency and directed proxy-log
tools are the network evidence sources. Real root-cause accuracy, unsampled path
completeness, subtype identification and 2-CPU/8-GB performance remain unproven.
