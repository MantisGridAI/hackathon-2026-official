"""Read-only telemetry benchmark for cold versus complete-window cache reuse.

Run from starter with --dataset and optionally --source. Prints measured JSON;
does not read development answers, call models, or write into the dataset.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import resource
import time

import pandas as pd

from agents.rca.contracts import QuerySpec, UTC8
from agents.rca.data_access import CSVTelemetryStore, SOURCES


def read(store, query):
    start = time.monotonic()
    digest = hashlib.sha256()
    count, first_component = 0, None
    for frame in store.iter_window(query, deadline=start + 180):
        count += len(frame)
        digest.update(pd.util.hash_pandas_object(frame, index=False,
                                               categorize=False).values.tobytes())
        if first_component is None and len(frame):
            first_component = frame._component_id.iloc[0]
    return {"elapsed_s": time.monotonic()-start, "rows": count,
            "sha256_row_hashes": digest.hexdigest(),
            "coverage": asdict(store.coverage(query)),
            "first_component": first_component}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--source", choices=tuple(SOURCES), default="trace_span")
    args = parser.parse_args()
    start = datetime(2022, 3, 20, 9, tzinfo=UTC8)
    broad = QuerySpec(args.source, start-timedelta(minutes=10, seconds=30),
                      start+timedelta(minutes=30, seconds=30), SOURCES[args.source][1])
    store = CSVTelemetryStore(args.dataset)
    cold = read(store, broad)
    component = cold.pop("first_component")
    narrow = replace(broad, start=start, end=start+timedelta(minutes=15),
                     component_ids=(component,) if component else None)
    warm = read(store, narrow)
    control = read(CSVTelemetryStore(args.dataset, cache_bytes=0), narrow)
    same = warm["rows"] == control["rows"] and warm["sha256_row_hashes"] == control["sha256_row_hashes"]
    report = {"source": args.source, "cold_covering_window": cold,
              "warm_filtered_subwindow": warm, "cold_filtered_control": control,
              "identical_row_values_and_record_locators": same,
              "cache_bytes": store._cache_size, "cache_limit_bytes": store.cache_bytes,
              "peak_rss_native_units": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
    print(json.dumps(report, default=str, indent=2))
    if not same or any(r["coverage"]["status"] != "complete" for r in (cold, warm, control)):
        raise SystemExit("Benchmark failed consistency or complete coverage")


if __name__ == "__main__":
    main()
