"""Bounded, provenance-preserving access to the eight official telemetry sources."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
import re
import time

import numpy as np
import pandas as pd

from .contracts import (Component, ComponentCatalog, Coverage, QuerySpec,
                        QueryValidationError, UTC8, stable_id)

SOURCES = {
    "metric_container": ("metric", ("timestamp", "cmdb_id", "kpi_name", "value")),
    "metric_node": ("metric", ("timestamp", "cmdb_id", "kpi_name", "value")),
    "metric_runtime": ("metric", ("timestamp", "cmdb_id", "kpi_name", "value")),
    "metric_mesh": ("metric", ("timestamp", "cmdb_id", "kpi_name", "value")),
    "metric_service": ("metric", ("service", "timestamp", "rr", "sr", "mrt", "count")),
    "trace_span": ("trace", ("timestamp", "cmdb_id", "span_id", "trace_id", "duration", "type", "status_code", "operation_name", "parent_span")),
    "log_service": ("log", ("log_id", "timestamp", "cmdb_id", "log_name", "value")),
    "log_proxy": ("log", ("log_id", "timestamp", "cmdb_id", "log_name", "value")),
}
LOCATORS = ("_source_file", "_record_index", "_timestamp_s", "_component_id")


@dataclass
class _CachedWindow:
    query: QuerySpec
    identity: tuple
    mapping_version: tuple
    columns: frozenset[str]
    frames: list[pd.DataFrame]
    coverage: Coverage
    size: int


class CSVTelemetryStore:
    def __init__(self, dataset_dir: Path, *, out_dir: Path | None = None,
                 chunk_size: int = 50000, cache_bytes: int = 256 * 1024 * 1024):
        self.dataset_dir = Path(dataset_dir).resolve()
        self.out_dir = Path(out_dir).resolve() if out_dir else None
        if chunk_size <= 0 or cache_bytes < 0:
            raise ValueError("Invalid store capacity")
        self.chunk_size, self.cache_bytes = chunk_size, cache_bytes
        self._catalog = ComponentCatalog()
        self._coverage: dict[str, Coverage] = {}
        self._cache = OrderedDict()
        self._cache_size = 0

    def _validate(self, query):
        if query.source not in SOURCES:
            raise QueryValidationError("Only official telemetry sources are allowed")
        columns = SOURCES[query.source][1]
        if not isinstance(query.columns, tuple) or len(set(query.columns)) != len(query.columns) or not set(query.columns) <= set(columns):
            raise QueryValidationError("Columns must be unique official raw column names")
        for bound in (query.start, query.end):
            if not isinstance(bound, datetime) or bound.tzinfo is None or bound.utcoffset() is None:
                raise QueryValidationError("Query times must be aware")
        if query.start >= query.end:
            raise QueryValidationError("Query interval must have positive duration")
        for values in (query.component_ids, query.operation_names, query.kpi_names):
            if values is not None and (not isinstance(values, tuple) or not all(isinstance(x, str) for x in values)):
                raise QueryValidationError("Filters must be tuples of strings or None")
        if query.operation_names is not None and query.source != "trace_span":
            raise QueryValidationError("Operation filter only supported by trace_span")
        if query.kpi_names is not None and "kpi_name" not in columns:
            raise QueryValidationError("KPI filter not supported by this source")

    def _paths(self, query):
        day = query.start.astimezone(UTC8).date()
        last = (query.end.astimezone(UTC8) - timedelta(microseconds=1)).date()
        while day <= last:
            relative = Path("telemetry") / day.strftime("%Y_%m_%d") / SOURCES[query.source][0] / (query.source + ".csv")
            path = self.dataset_dir / relative
            # Resolve before touching any content, including symlinks/junctions.
            if not path.resolve().is_relative_to(self.dataset_dir):
                raise QueryValidationError("Telemetry path escapes dataset")
            yield path, relative.as_posix()
            day += timedelta(days=1)

    def query_id(self, query):
        self._validate(query)
        return stable_id(str(self.dataset_dir), "query", {"query": asdict(query), "version": "csv.v1"})

    def coverage(self, query):
        qid = self.query_id(query)
        return deepcopy(self._coverage.get(qid, Coverage(qid, query.source)))

    def component_catalog(self):
        result = deepcopy(self._catalog)
        result.coverage = [deepcopy(v) for v in self._coverage.values()]
        return result

    def _register(self, source, raw, component, kind, filename, node=None, service=None):
        previous = self._catalog.components.get(component)
        self._catalog.components[component] = Component(component, kind,
            tuple(sorted(set((previous.raw_ids if previous else ()) + (raw,)))),
            node or (previous.node_id if previous else None),
            service or (previous.service if previous else None),
            tuple(sorted(set((previous.source_files if previous else ()) + (filename,)))))
        key = (source, raw)
        self._catalog.raw_to_components[key] = tuple(sorted(set(self._catalog.raw_to_components.get(key, ()) + (component,))))

    def _observe(self, source, raw, filename):
        if not raw or raw in {"nan", "<NA>"}:
            return ()
        if source == "metric_node":
            self._register(source, raw, raw, "node", filename)
            return (raw,)
        if source in {"metric_container", "trace_span", "log_service", "log_proxy"}:
            node, pod = None, raw
            if source == "metric_container":
                if "." not in raw:
                    return ()  # Never pretend an undocumented container ID split is known.
                node, pod = raw.split(".", 1)
            elif raw.startswith("node-") and "." in raw:
                node, pod = raw.split(".", 1)
            elif raw in self._catalog.components and self._catalog.components[raw].kind == "node":
                self._register(source, raw, raw, "node", filename)
                return (raw,)
            service_match = re.fullmatch(r"(.+)-\d+", pod)
            service = service_match[1] if service_match else None
            self._register(source, raw, pod, "container", filename, node, service)
            if node:
                self._register(source, node, node, "node", filename)
            return (pod,)
        if source == "metric_mesh":
            parts = raw.split(".")
            observed = set()
            # The prefix is an observed pod; suffixes are service endpoints, not pod IDs.
            if len(parts) >= 4 and parts[1] in {"source", "destination"}:
                pod = parts[0]
                service_match = re.fullmatch(r"(.+)-\d+", pod)
                self._register(source, raw, pod, "container", filename,
                               service=service_match[1] if service_match else None)
                observed.add(pod)
                endpoint_services = set(parts[2:])
                observed.update(c.component_id for c in self._catalog.components.values() if c.service in endpoint_services)
            result = tuple(sorted(observed))
        else:
            service = raw if source == "metric_service" else raw.split(":", 1)[0].split(".", 1)[0]
            result = tuple(sorted(c.component_id for c in self._catalog.components.values() if c.service == service))
        self._catalog.raw_to_components[(source, raw)] = result
        return result

    def iter_window(self, query, *, deadline):
        self._validate(query)
        # Keep validation eager even though iteration is lazy.
        paths = list(self._paths(query))
        return self._iterate(query, paths, deadline)

    @staticmethod
    def _required_columns(query):
        required = set(query.columns) | {"timestamp", "service" if query.source == "metric_service" else "cmdb_id"}
        if query.operation_names is not None:
            required.add("operation_name")
        if query.kpi_names is not None:
            required.add("kpi_name")
        return required

    @staticmethod
    def _file_identity(paths):
        entries = []
        for path, relative in paths:
            try:
                stat = path.stat()
            except FileNotFoundError:
                entries.append((relative, None, None, None, None))
            else:
                entries.append((relative, stat.st_size, stat.st_mtime_ns,
                                stat.st_ctime_ns, stat.st_ino))
        return tuple(entries)

    def _covering_cache(self, query, identity, mapping_version, required):
        """Reuse only proven-complete supersets of the requested rows/columns.

        A file identity is checked again on every read. Source rows stay in their
        original CSV order, including out-of-order times and multiline records.
        A filtered window can cover only an equally/more restrictive filter.
        """
        for key in reversed(self._cache):
            entry = self._cache[key]
            old = entry.query
            if old.source != query.source or old.start > query.start or old.end < query.end:
                continue
            if not required <= entry.columns or not set(identity) <= set(entry.identity):
                continue
            if old.component_ids is not None and entry.mapping_version != mapping_version:
                continue
            for before, after in ((old.component_ids, query.component_ids),
                                  (old.operation_names, query.operation_names),
                                  (old.kpi_names, query.kpi_names)):
                if before is not None and (after is None or not set(after) <= set(before)):
                    break
            else:
                self._cache.move_to_end(key)
                return entry
        return None

    def _filter_components(self, frame, query, relative, cov):
        raw_column = "service" if query.source == "metric_service" else "cmdb_id"
        mappings = {raw: self._observe(query.source, str(raw), relative) for raw in frame[raw_column].unique()}
        if query.source in {"metric_service", "metric_mesh", "metric_runtime"}:
            frame["_component_id"] = None
        else:
            frame["_component_id"] = frame[raw_column].map(lambda x: mappings[x][0] if len(mappings[x]) == 1 else None)
        if query.component_ids is not None:
            wanted = set(query.component_ids)
            if wanted and any(not x for x in mappings.values()):
                warning = "Some component relationships are unknown; component filter cannot be fully resolved"
                if warning not in cov.warnings:
                    cov.warnings.append(warning)
            frame = frame.loc[frame[raw_column].map(lambda raw: bool(wanted.intersection(mappings[raw])))]
            if query.source in {"metric_service", "metric_runtime", "metric_mesh"} and wanted:
                warning = "Service/mesh observations aggregate endpoints and cannot isolate a single pod"
                if warning not in cov.warnings:
                    cov.warnings.append(warning)
        if query.operation_names is not None:
            frame = frame.loc[frame.operation_name.isin(query.operation_names)]
        if query.kpi_names is not None:
            frame = frame.loc[frame.kpi_name.isin(query.kpi_names)]
        return frame

    @staticmethod
    def _count_output(output, query, cov):
        cov.rows_matched += len(output)
        missing_values = output[list(query.columns)].isna() | output[list(query.columns)].eq("")
        cov.missing_value_count = (cov.missing_value_count or 0) + int(missing_values.sum().sum())
        first = datetime.fromtimestamp(float(output._timestamp_s.min()), UTC8)
        last = datetime.fromtimestamp(float(output._timestamp_s.max()), UTC8)
        cov.first_time = min(cov.first_time, first) if cov.first_time else first
        cov.last_time = max(cov.last_time, last) if cov.last_time else last

    def _iterate(self, query, paths, deadline):
        qid = self.query_id(query)
        cov = Coverage(qid, query.source, status="partial")
        self._coverage[qid] = cov
        identity = self._file_identity(paths)
        # Catalog identity matters for service/mesh filtering, which is observation-dependent.
        mapping_version = tuple(sorted((k, v.service, v.node_id) for k, v in self._catalog.components.items())) if query.source in {"metric_service", "metric_mesh", "metric_runtime"} else ()
        key = (qid, identity, mapping_version)
        required = self._required_columns(query)
        cache_frames, cache_size = [], 0
        missing, files_read, finished = [], 0, False
        try:
            if time.monotonic() >= deadline:
                cov.warnings.append("Deadline reached before scan")
                return
            cached = self._covering_cache(query, identity, mapping_version, required)
            if cached is not None:
                for stored in cached.frames:
                    if time.monotonic() >= deadline:
                        cov.warnings.append("Deadline reached while reading cached chunks")
                        return
                    cov.rows_scanned += len(stored)
                    mask = stored._timestamp_s.ge(query.start.timestamp()) & stored._timestamp_s.lt(query.end.timestamp())
                    frame = stored.loc[mask].copy(deep=True)
                    if frame.empty:
                        continue
                    frame = self._filter_components(frame, query, str(frame._source_file.iloc[0]), cov)
                    if frame.empty:
                        continue
                    output = frame.loc[:, list(query.columns) + list(LOCATORS)].reset_index(drop=True)
                    self._count_output(output, query, cov)
                    yield output
                if self._file_identity(paths) != identity:
                    cov.warnings.append("Telemetry files changed during cache read; complete coverage cannot be established")
                    cov.status = "partial"
                    finished = True
                    return
                unresolved = any("cannot be fully resolved" in w for w in cov.warnings)
                cov.status = "partial" if unresolved else ("complete" if cov.rows_matched else "empty")
                # The complete superset scan proved coverage of these unchanged
                # files; this count is logical source coverage, not fresh I/O.
                cov.rows_scanned = cached.coverage.rows_scanned
                # Cache hits do not change observational coverage or stable IDs.
                # I/O timing belongs in tool diagnostics, not evidence warnings.
                finished = True
                return
            for path, relative in paths:
                if time.monotonic() >= deadline:
                    cov.warnings.append("Deadline reached between files")
                    return
                if not path.exists():
                    missing.append(relative)
                    continue
                # Preserve trace/log IDs verbatim, e.g. leading zeros. Numeric columns are parsed explicitly.
                reader = pd.read_csv(path, usecols=list(required), chunksize=self.chunk_size, dtype=str,
                                     keep_default_na=False, encoding="utf-8")
                record_offset = 0
                files_read += 1
                try:
                    for frame in reader:
                        if time.monotonic() >= deadline:
                            cov.warnings.append("Deadline reached during scan")
                            return
                        length = len(frame)
                        cov.rows_scanned += length
                        timestamps = pd.to_numeric(frame["timestamp"], errors="coerce")
                        normalized = timestamps / (1000 if query.source == "trace_span" else 1)
                        mask = normalized.ge(query.start.timestamp()) & normalized.lt(query.end.timestamp()) & np.isfinite(normalized)
                        frame["_record_index"] = np.arange(record_offset + 1, record_offset + length + 1)
                        frame["_timestamp_s"] = normalized
                        record_offset += length
                        frame = frame.loc[mask].copy()
                        if frame.empty:
                            continue
                        frame = self._filter_components(frame, query, relative, cov)
                        if frame.empty:
                            continue
                        frame["_source_file"] = relative
                        # Raw timestamp/duration units are untouched; numeric values remain raw magnitudes.
                        for col in set(frame.columns) & {"timestamp", "duration", "rr", "sr", "mrt", "count"}:
                            frame[col] = pd.to_numeric(frame[col], errors="coerce")
                        if query.source.startswith("metric") and "value" in frame:
                            frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
                        output = frame.loc[:, list(query.columns) + list(LOCATORS)].reset_index(drop=True)
                        self._count_output(output, query, cov)
                        # Keep the raw columns needed for safe projection/filter
                        # reuse, even when the original caller omitted them.
                        retained = frame.loc[:, sorted(required) + list(LOCATORS)].reset_index(drop=True)
                        cache_size += int(retained.memory_usage(index=True, deep=True).sum())
                        if cache_size <= self.cache_bytes:
                            cache_frames.append(retained.copy(deep=True))
                        else:
                            cache_frames.clear()
                        yield output
                finally:
                    reader.close()
            cov.warnings.extend("Missing file: " + name for name in missing)
            if self._file_identity(paths) != identity:
                cov.warnings.append("Telemetry files changed during scan; complete coverage cannot be established")
                cov.status = "partial"
                finished = True
                return
            unresolved = any("cannot be fully resolved" in w for w in cov.warnings)
            cov.status = ("partial" if files_read else "missing") if missing else ("partial" if unresolved else ("complete" if cov.rows_matched else "empty"))
            finished = True
            if not missing and not unresolved and self.cache_bytes and cache_size <= self.cache_bytes:
                while self._cache and (self._cache_size + cache_size > self.cache_bytes or len(self._cache) >= 128):
                    _, previous = self._cache.popitem(last=False)
                    self._cache_size -= previous.size
                self._cache[key] = _CachedWindow(query, identity, mapping_version,
                    frozenset(required), cache_frames, deepcopy(cov), cache_size)
                self._cache_size += cache_size
        except (OSError, ValueError, pd.errors.ParserError) as exc:
            cov.status = "failed"
            cov.warnings.append("Telemetry read failed: " + type(exc).__name__)
            finished = True
        finally:
            if not finished:
                cov.status = "partial"
                cov.warnings.extend("Missing file: " + name for name in missing)
                if not cov.warnings:
                    cov.warnings.append("Iterator closed before scan completed")
