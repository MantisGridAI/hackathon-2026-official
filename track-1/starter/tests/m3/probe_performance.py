"""Telemetry-only, opt-in performance probe; never reads development labels."""
import argparse
import csv
import json
from pathlib import Path
import resource
import time

from agents.rca.data_access import CSVTelemetryStore
from agents.rca.runtime import parse_case
from agents.rca.traces import triage_traces


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--rows", default="0,1")
    args = parser.parse_args()
    with (args.dataset / "query.csv").open(newline="", encoding="utf-8") as handle:
        queries = list(csv.DictReader(handle))
    reports = []
    for row in map(int, args.rows.split(",")):
        case = parse_case(queries[row]["instruction"])
        store = CSVTelemetryStore(args.dataset, out_dir=args.out.parent / "cache")
        started = time.monotonic()
        bundle = triage_traces(case, store, deadline=started + args.seconds)
        report = {"row_position": row, "case_key": case.case_key,
                  "elapsed_s": round(time.monotonic() - started, 4),
                  "budget_s": args.seconds, "peak_rss_native": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                  "coverage": [vars(c) for c in bundle.coverage], "warnings": bundle.warnings,
                  "evidence": len(bundle.evidence), "edges": len(bundle.edges),
                  "candidates": len(bundle.candidates),
                  "pairing": [r.values for r in bundle.evidence if "pairing_quality" in r.transform],
                  "group_span_totals": {period: sum(r.values[period]["span_count"] for r in bundle.evidence
                      if "group_compare" in r.transform) for period in ("baseline", "incident")}}
        reports.append(report)
        print(json.dumps(report, default=str), flush=True)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(reports, indent=2, default=str) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
