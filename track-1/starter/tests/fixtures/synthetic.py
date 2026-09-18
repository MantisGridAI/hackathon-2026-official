"""Tiny fabricated dataset for end-to-end wiring tests, not an accuracy benchmark."""
import csv
from datetime import datetime, timedelta
from pathlib import Path

from agents.rca.contracts import UTC8
from agents.rca.data_access import SOURCES


def make_dataset(destination: Path):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    start = datetime(2022, 3, 20, 9, tzinfo=UTC8)
    tables = {name: [] for name in SOURCES}
    for minute in range(-10, 30):
        timestamp = (start + timedelta(minutes=minute)).timestamp()
        changed = 5 <= minute <= 18
        for pod in ("worker-1", "worker-2", "front-1"):
            for kpi, value in (("container_fs_reads_MB", 100 if changed and pod == "worker-1" else 1),
                               ("container_cpu_usage", 3 if changed and pod == "worker-1" else 1),
                               ("container_memory_usage", 1)):
                tables["metric_container"].append([timestamp, "node-synthetic."+pod, kpi, value])
        tables["metric_node"].append([timestamp, "node-synthetic", "system.cpu.user", 2 if changed else 1])
        tables["metric_service"].append(["worker", timestamp, 100, 100, 10, 2])
        trace_id = "synthetic-" + str(minute)
        tables["trace_span"].append([timestamp*1000, "front-1", "parent-"+str(minute), trace_id, 20, "rpc", "0", "Get", ""])
        tables["trace_span"].append([timestamp*1000+(5 if changed else 1), "worker-1", "child-"+str(minute), trace_id, 10, "rpc", "0", "Get", "parent-"+str(minute)])
        tables["log_service"].append(["synthetic-log-"+str(minute), timestamp, "worker-1", "service", "timeout" if changed else "request complete"])
    for name, rows in tables.items():
        folder, columns = SOURCES[name]
        path = destination / "telemetry" / "2022_03_20" / folder / (name+".csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(columns)
            writer.writerows(rows)
    with (destination / "query.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["row_id", "task_index", "instruction"])
        writer.writerow([7, "task_6", "The synthetic system experienced one failure on March 20, 2022, from 09:00 to 09:30. You are tasked with identifying the root cause component and root cause reason."])
        writer.writerow([23, "task_7", "The synthetic system experienced two failures on March 20, 2022, from 09:00 to 09:30. You are tasked with identifying the root cause occurrence datetime, root cause component and root cause reason."])
    (destination / "SYNTHETIC.txt").write_text("FABRICATED TEST TELEMETRY. NOT REAL RCA EVIDENCE.\n", encoding="utf-8")
    return destination
