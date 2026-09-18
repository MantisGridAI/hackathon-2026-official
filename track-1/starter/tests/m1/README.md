M1 implements rca-v1 in `agents.rca.contracts`, `runtime`, and `data_access`.
Consumers use the Protocol; no model SDK or credentials are needed to import it.

Example from the starter directory:

```python
from pathlib import Path
import time
from agents.rca.runtime import parse_case
from agents.rca.contracts import QuerySpec
from agents.rca.data_access import CSVTelemetryStore

case = parse_case("One failure March 20, 2022 from 09:00 to 09:30. "
                  "You are tasked with identifying the root cause component.")
store = CSVTelemetryStore(Path("../data/Market-cloudbed-1"))
query = QuerySpec("metric_node", case.start, case.end,
                  ("timestamp", "cmdb_id", "kpi_name", "value"))
iterator = store.iter_window(query, deadline=time.monotonic() + 10)
try:
    for frame in iterator:
        print(frame.columns)  # requested columns + four original-source locators
finally:
    iterator.close()
print(store.coverage(query))
```

Checks include all seven requested-field combinations, every official label-free
instruction, UTC+8/cross-midnight windows, seconds vs milliseconds, quoted/multiline
CSV, unsorted records, original indexes, partial/missing/empty/failed scans, early
close/deadlines, component mappings and state reuse. Run:

```bash
python -m unittest discover -s tests/m1 -p 'test_*.py'
```

Without `RCA_TEST_DATA`, the real smoke test explicitly skips. With it, the test
verifies the first real node-window record independently through CSV record indexing.
The component catalog is incremental; service/mesh relationships that cannot be
resolved are disclosed. Small in-memory query results use a bounded 32 MiB cache
keyed by query, file size/mtime, mapping state and transform version. The reader does
not assume time ordering and does not index or materialize entire telemetry days.

Default-entrypoint, actual-SDK local-HTTP, read-only dataset, noncontinuous row IDs,
atomic prediction saving and resume provenance tests live in `tests/integration`.
Actual full-run/native resource measurements and Docker limitations are in REPORT.md.
