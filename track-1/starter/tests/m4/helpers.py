"""Tiny synthetic cases, transport responses and telemetry bundles."""
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import time

from agents.rca.contracts import (AnalysisBundle, Candidate, CaseContext, Component,
    ComponentCatalog, Coverage, EvidenceRecord, RunConfig, RunState, UTC8)
from llm import AttemptResult


def case(count=1):
    start = datetime(2026, 1, 1, 9, tzinfo=UTC8)
    return CaseContext("synthetic-case", "Synthetic fixture, not real evidence", start,
                       start + timedelta(minutes=30), start - timedelta(minutes=10),
                       count, ("datetime", "component", "reason"))


def evidence(key="e1", component="synthetic-pod", kind="metric", status="complete"):
    c = case()
    return EvidenceRecord(key, kind, [component], (c.start, c.end),
        source_files=["telemetry/2026-01-01/metric/metric_container.csv"],
        transform="synthetic.fixture.v1", values={"value": 2.},
        units={"value": "unknown"}, coverage=[Coverage("q", "metric_container", status,
                                                                    rows_matched=10)])


def candidate(key="c1", component="synthetic-pod", reason="container CPU load", eid="e1"):
    c = case()
    return Candidate(key, component, reason, (c.start, c.start + timedelta(seconds=30)),
                     c.start + timedelta(seconds=15),
                     {"metrics.strength": 12., "metrics.family": "cpu",
                      "metrics.persistence_samples": 4}, [eid], episode_id=component + ":episode")


class Store:
    def __init__(self):
        self.catalog = ComponentCatalog({"synthetic-pod": Component("synthetic-pod", "container"),
            "synthetic-peer": Component("synthetic-peer", "container")})

    def component_catalog(self):
        return self.catalog


def state(out, **config):
    now = time.monotonic()
    return RunState(Store(), RunConfig(**config), Path(out), now, now + 120, invocation_index=1)


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, model, messages, **kwargs):
        self.calls.append((model, kwargs))
        response = self.responses.pop(0)
        return response(model) if callable(response) else response


def response(model, text="{}", pt=20, ct=10, error=None):
    return AttemptResult(model, text, pt, ct, error)


def bundle():
    return AnalysisBundle("m2", [candidate()], [evidence()])
