"""The rca-v1 contracts; see docs/INTERFACES.md for semantics."""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Iterator, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

UTC8 = timezone(timedelta(hours=8))
SCHEMA_VERSION = "rca-v1"
NODE_REASONS = frozenset({"node CPU load", "node CPU spike", "node disk read I/O consumption",
    "node disk space consumption", "node disk write I/O consumption", "node memory consumption"})
CONTAINER_REASONS = frozenset({"container CPU load", "container memory load", "container network latency",
    "container network packet corruption", "container network packet retransmission", "container packet loss",
    "container process termination", "container read I/O load", "container write I/O load"})
LEGAL_REASONS = NODE_REASONS | CONTAINER_REASONS


class CaseParseError(ValueError):
    """The instruction does not contain a reliable time window."""


class QueryValidationError(ValueError):
    """A query requests a source, column or filter not supported by the store."""


def _json_value(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Stable IDs require aware datetimes")
        return value.isoformat()
    if is_dataclass(value):
        return {f.name: _json_value(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {k: _json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(v) for v in value]
    return value


def stable_id(case_key: str, namespace: str, payload: dict) -> str:
    encoded = json.dumps({"case_key": case_key, "payload": _json_value(payload)},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return namespace + ":" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


@dataclass
class CaseContext:
    case_key: str
    instruction: str
    start: datetime
    end: datetime
    reference_start: datetime
    failure_count: int
    requested_fields: tuple[str, ...]
    parse_status: str = "ok"
    parse_warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class QuerySpec:
    source: str
    start: datetime
    end: datetime
    columns: tuple[str, ...]
    component_ids: tuple[str, ...] | None = None
    operation_names: tuple[str, ...] | None = None
    kpi_names: tuple[str, ...] | None = None


@dataclass
class Coverage:
    query_id: str
    source: str
    status: str = "not_queried"
    rows_scanned: int = 0
    rows_matched: int = 0
    first_time: datetime | None = None
    last_time: datetime | None = None
    missing_value_count: int | None = None
    duplicate_key_count: int | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class Component:
    component_id: str
    kind: str
    raw_ids: tuple[str, ...] = ()
    node_id: str | None = None
    service: str | None = None
    source_files: tuple[str, ...] = ()


@dataclass
class ComponentCatalog:
    components: dict[str, Component] = field(default_factory=dict)
    raw_to_components: dict[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    coverage: list[Coverage] = field(default_factory=list)


class TelemetryStore(Protocol):
    def iter_window(self, query: QuerySpec, *, deadline: float) -> Iterator[pd.DataFrame]: ...
    def query_id(self, query: QuerySpec) -> str: ...
    def coverage(self, query: QuerySpec) -> Coverage: ...
    def component_catalog(self) -> ComponentCatalog: ...


@dataclass
class SourceRecord:
    source_file: str
    record_index: int | None = None
    trace_id: str | None = None
    span_id: str | None = None
    log_id: str | None = None


@dataclass
class EvidenceRecord:
    evidence_id: str
    kind: str
    component_ids: list[str]
    interval: tuple[datetime, datetime]
    source_files: list[str] = field(default_factory=list)
    queries: list[QuerySpec] = field(default_factory=list)
    transform: str = ""
    transform_params: dict = field(default_factory=dict)
    values: dict = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    source_records: list[SourceRecord] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    candidate_id: str
    component: str
    reason: str | None = None
    onset_interval: tuple[datetime, datetime] | None = None
    onset_estimate: datetime | None = None
    features: dict = field(default_factory=dict)
    supporting_ids: list[str] = field(default_factory=list)
    contradicting_ids: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    episode_id: str | None = None


@dataclass
class DependencyEdge:
    edge_id: str
    caller: str
    callee: str
    operation: str | None = None
    supporting_ids: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


@dataclass
class AnalysisBundle:
    module: str
    candidates: list[Candidate] = field(default_factory=list)
    evidence: list[EvidenceRecord] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)
    edges: list[DependencyEdge] = field(default_factory=list)
    suggested_queries: list[QuerySpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class RunConfig:
    schema_version: str = SCHEMA_VERSION
    mode: str = "routed"
    pinned_model: str | None = None
    reference_minutes: int = 10
    case_soft_seconds: float = 55.0
    run_soft_seconds: float = 1140.0
    case_cost_limit_usd: float = 2.5
    run_cost_limit_usd: float = 20.0
    max_followups: int = 1
    max_model_stages: int = 2
    max_http_attempts_per_case: int = 4


@dataclass
class RunState:
    store: TelemetryStore
    config: RunConfig
    out_dir: Path
    started_monotonic: float
    deadline: float
    invocation_index: int = 0
    client: object | None = None
    model_health: dict = field(default_factory=dict)
    usage_ledger: dict = field(default_factory=dict)
    estimated_cost_usd: float = 0.0


@dataclass
class Answer:
    datetime: datetime | None = None
    component: str | None = None
    reason: str | None = None


@dataclass
class Alternative:
    candidate_id: str
    status: str
    evidence_ids: list[str] = field(default_factory=list)
    explanation: str = ""


@dataclass
class Decision:
    answers: list[Answer]
    selected_candidate_ids: list[str] = field(default_factory=list)
    confidence: str = "low"
    supporting_ids: list[str] = field(default_factory=list)
    alternatives: list[Alternative] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    stop_reason: str = ""
    route_events: list[dict] = field(default_factory=list)
    usage_delta: dict = field(default_factory=dict)


@dataclass
class InvestigationResult:
    decision: Decision
    fallback: Decision
    evidence: list[EvidenceRecord] = field(default_factory=list)


@dataclass
class RenderedResult:
    prediction: str
    evidence: str
    validation_status: str
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
