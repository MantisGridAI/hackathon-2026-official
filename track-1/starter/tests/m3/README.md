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

Trace reads use one complete covering query from the reference period through
the incident, with 30 seconds of context at both ends. There is no first-150k-row,
512-group or 256-edge cutoff. Input memory is guarded at 256 MiB and the caller's
deadline still applies; these exceptional safety exits are explicitly partial.
Repeated metadata uses shared categorical dictionaries across chunks. Vectorized
unique-key pairing retains integer source positions instead of several
copies of every span dictionary. All groups and edges are analyzed when the input
and time fit the bounds. `retained_rows_per_query` records the exact consumed input
for replay; a replay that cannot recover it fails explicitly. New transforms use
`.v2`; the old two-query, prefix-limited `.v1` records still replay. No self-time is
derived by subtracting child durations.

Log searches use case-insensitive literal substrings; regex-like input stays
literal. They analyze the complete filtered window (128 MiB input safety guard)
and keep eight positioned matching samples, including neighboring same-component
records. Original text is preserved
up to an explicit 2,000-character limit. Empty, missing, not-queried and partial
are distinct. Compact results for eight completed signatures per store avoid
repeating identical searches during an immutable dataset run.

## Current performance repair checks

On a native Apple laptop, the offline synthetic suite passes 18 tests; the real
smoke is opt-in. New regressions cover full-window reads beyond the old prefix
limit, all 260 synthetic edges beyond the old edge cap, memory-limit disclosure,
v1/v2 replay, duplicate child keys, self-parent keys and vectorized/legacy pairing
equivalence. No paid API is involved.

Two real label-free windows were independently measured using
`tests/m3/probe_performance.py`, with the original CSV store unchanged:

| Query position | Previous trace tool | Repaired trace tool | Completeness |
|---|---:|---:|---|
| 0 | 18.05 s | 8.81 s | All 132,609 spans; 272 edges instead of 256 |
| 1 | 17.20 s | 9.13 s | All 194,925 spans instead of 187,135; 272 edges |

Both repaired windows have complete scan coverage and no analysis warnings. The
second window's 5,750 artificially missing parents disappeared when its truncated
incident prefix was replaced by the complete input. Old runs had a 60-second
budget; final repaired runs had 20 seconds, and all four finished below 20 seconds,
so neither result was determined by those deadlines.

An additional high-traffic window (position 20) has 443,694 spans and took 10.16 s;
a window on the other day (position 53) has 343,474 spans and took 9.79 s. Both have
complete scan coverage and all 272 edges. The high-traffic window originally
exceeded the raw-string memory guard; dictionary compression preserves all rows
under the same guard, rather than increasing an arbitrary row limit. The other
day retains 49 genuinely absent parents within the padded input; complete scan
coverage does not assert complete tracing. Peak RSS across the final four-window
process was 577.1 MB (macOS reports bytes), not a 2-CPU container result.
The local probe JSON files under `out/` retain coverage, exact counts and timings.

A real directed `log_service` search for `cartservice-2` in the first window
scanned 4,645,099 file rows, retained all 10,314 matching component/window records
and completed in 2.94 seconds with complete coverage. The six literal patterns
(`error`, `timeout`, `reset`, `refused`, `killed`, `oom`) had zero matches; this is
not a health claim. Replay reproduced the values exactly from the cached input.

```bash
PYTHONPATH=. python tests/m3/probe_performance.py \
  --dataset "$RCA_TEST_DATA" --out "$RCA_OUT/m3-probe.json" --rows 0,1 --seconds 30
```

## Historical checks before the performance repair

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
