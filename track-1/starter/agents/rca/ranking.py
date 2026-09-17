"""Bounded, provenance-preserving ranking; scores are not probabilities."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import timedelta
import math

from .contracts import (Alternative, Answer, Candidate, CONTAINER_REASONS,
                        Decision, LEGAL_REASONS, NODE_REASONS, stable_id)


class EvidenceCollision(ValueError):
    """One stable ID denotes different content; never silently overwrite it."""


def merge_bundles(bundles):
    candidates, evidence, edges, warnings = {}, {}, {}, []
    for bundle in bundles:
        for items, field, target in ((bundle.evidence, "evidence_id", evidence),
                                     (bundle.candidates, "candidate_id", candidates),
                                     (bundle.edges, "edge_id", edges)):
            for item in items:
                key = getattr(item, field)
                if key in target and target[key] != item:
                    raise EvidenceCollision("Conflicting " + field + ": " + key)
                target[key] = item
        warnings.extend(bundle.warnings)
        for coverage in bundle.coverage:
            if coverage.status != "complete":
                warnings.append(f"{coverage.source}: coverage {coverage.status}; not evidence of health")
    return list(candidates.values()), list(evidence.values()), list(edges.values()), list(dict.fromkeys(warnings))


def _number(features, key, default=0.):
    value = features.get(key)
    return float(value) if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) else default


def _compatible(left, right):
    if left.component != right.component or left.reason != right.reason:
        return False
    if left.onset_interval is not None and right.onset_interval is not None:
        return max(left.onset_interval[0], right.onset_interval[0]) <= min(left.onset_interval[1], right.onset_interval[1])
    return left.episode_id == right.episode_id


def prepare_candidates(case, candidates, catalog, evidence):
    known_ids = {record.evidence_id for record in evidence}
    expanded = []
    for original in candidates:
        candidate = deepcopy(original)
        if set(candidate.supporting_ids + candidate.contradicting_ids) - known_ids:
            raise EvidenceCollision("Candidate contains an unknown evidence reference")
        observed = catalog.components.get(candidate.component)
        legal = NODE_REASONS if observed and observed.kind == "node" else CONTAINER_REASONS
        if candidate.reason is not None and (candidate.reason not in LEGAL_REASONS or
                                             observed and candidate.reason not in legal):
            continue
        if candidate.reason is None and "reason" in case.requested_fields:
            if candidate.features.get("traces.network_family"):
                reasons = [r for r in sorted(legal) if "network" in r or "packet" in r]
                reasons = reasons or sorted(legal)
            else:
                reasons = sorted(legal)
            for reason in reasons:
                cid = stable_id(case.case_key, "m4.candidate", {
                    "parent": candidate.candidate_id, "reason": reason, "transform": "weak_hypothesis.v1"})
                expanded.append(replace(candidate, candidate_id=cid, reason=reason,
                    unresolved=candidate.unresolved + ["Reason is a weak legal hypothesis, not a mechanism-specific observation"],
                    features={**candidate.features, "m4.weak_reason": True,
                              "m4.provenance": [candidate.candidate_id]}))
        else:
            candidate.features["m4.provenance"] = [candidate.candidate_id]
            expanded.append(candidate)
    groups = []
    for candidate in sorted(expanded, key=lambda c: c.candidate_id):
        existing = next((c for c in groups if _compatible(c, candidate)), None)
        if existing is None:
            groups.append(candidate)
            continue
        # Correlated KPIs contribute their maximum, never their sum.
        for key, value in candidate.features.items():
            if key not in existing.features:
                existing.features[key] = value
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                previous = existing.features[key]
                if isinstance(previous, (int, float)):
                    existing.features[key] = max(previous, value)
        for attr in ("supporting_ids", "contradicting_ids", "unresolved"):
            setattr(existing, attr, sorted(set(getattr(existing, attr) + getattr(candidate, attr))))
        existing.features["m4.provenance"] = sorted(set(existing.features["m4.provenance"] + candidate.features["m4.provenance"]))
        existing.candidate_id = stable_id(case.case_key, "m4.candidate", {
            "parents": existing.features["m4.provenance"], "transform": "candidate_fusion.v1"})
    return groups


@dataclass
class Ranked:
    candidate: Candidate
    score: float
    contributions: dict


def rank_candidates(case, candidates, evidence):
    evidence_by_id = {e.evidence_id: e for e in evidence}
    result = []
    for candidate in candidates:
        f = candidate.features
        support = [evidence_by_id[i] for i in candidate.supporting_ids if i in evidence_by_id]
        parts = {
            "metric_strength": min(5., max(0., _number(f, "metrics.strength")) / 4.),
            "trace_strength": min(3., max(0., _number(f, "traces.anomaly_score"))),
            "network_strength": min(2., max(0., _number(f, "network.anomaly_score"))),
            "log_strength": min(1., max(0., _number(f, "logs.anomaly_score"))),
            "sustained": min(1., max(0., _number(f, "metrics.persistence_samples") - 1) / 4.),
            "replica_contrast": min(1., max(0., _number(f, "metrics.replica_contrast")) / 4.),
            "contradiction": -min(3., len(set(candidate.contradicting_ids))),
            "weak_reason": -.25 if f.get("m4.weak_reason") else 0.,
        }
        # No source absence penalty: unobserved is not healthy.
        kinds = {e.kind for e in support if any(c.rows_matched > 0 for c in e.coverage)}
        parts["independent_sources"] = .5 * max(0, len(kinds - {"coverage"}) - 1)
        parts["earlier_separable_onset"] = 0.
        if candidate.onset_interval:
            competitors = [other for other in candidates if other.component != candidate.component and other.onset_interval]
            if competitors and all(candidate.onset_interval[1] < c.onset_interval[0] for c in competitors):
                parts["earlier_separable_onset"] = .5
        result.append(Ranked(candidate, round(sum(parts.values()), 6), parts))
    return sorted(result, key=lambda item: (-item.score, item.candidate.component,
                 item.candidate.reason or "", item.candidate.candidate_id))


def _same_episode(left, right):
    if left.component == right.component:
        if left.onset_interval and right.onset_interval:
            return max(left.onset_interval[0], right.onset_interval[0]) <= min(left.onset_interval[1], right.onset_interval[1])
        return True
    return left.episode_id is not None and left.episode_id == right.episode_id


def choose_candidates(ranked, count, edges=()):
    """Prefer distinguishable episodes; dependency direction proves no causality."""
    chosen, deferred = [], []
    connected = {frozenset((e.caller, e.callee)) for e in edges}
    for item in ranked:
        c = item.candidate
        if any(_same_episode(c, previous) for previous in chosen):
            continue
        related = any(frozenset((c.component, previous.component)) in connected and
                      c.onset_interval and previous.onset_interval and
                      max(c.onset_interval[0], previous.onset_interval[0]) <= min(c.onset_interval[1], previous.onset_interval[1])
                      for previous in chosen)
        if related:
            deferred.append(c)
            continue
        chosen.append(c)
        if len(chosen) == count:
            return chosen
    for c in deferred:
        if not any(_same_episode(c, previous) for previous in chosen):
            chosen.append(c)
        if len(chosen) == count:
            break
    return chosen


def deterministic_gate(case, ranked, selected, evidence):
    """Conservative until accuracy/coverage thresholds have real validation."""
    if len(selected) != case.failure_count or not selected:
        return False
    by_id = {e.evidence_id: e for e in evidence}
    for candidate in selected:
        if candidate.unresolved or candidate.contradicting_ids or not candidate.features.get("mechanism.direct"):
            return False
        records = [by_id[i] for i in candidate.supporting_ids if i in by_id]
        if not records or any(not e.coverage or any(c.status != "complete" for c in e.coverage) for e in records):
            return False
        selected_score = next(r.score for r in ranked if r.candidate.candidate_id == candidate.candidate_id)
        alternatives = [r.score for r in ranked if r.candidate.candidate_id not in {s.candidate_id for s in selected}]
        if alternatives and selected_score - max(alternatives) < 2.:
            return False
    return True


def make_decision(case, selected, ranked, catalog, *, warnings=(), stop_reason="best_guess",
                  confidence="low"):
    answers, limitations = [], list(warnings)
    for candidate in selected[:case.failure_count]:
        onset = candidate.onset_estimate
        if onset is None:
            onset = case.start
            limitations.append("Onset unavailable: window start is an unverified best guess")
        onset = max(case.start, min(onset, case.end - timedelta(seconds=1)))
        answers.append(Answer(onset if "datetime" in case.requested_fields else None,
                              candidate.component if "component" in case.requested_fields else None,
                              candidate.reason if "reason" in case.requested_fields else None))
        limitations.extend(candidate.unresolved)
    for component in sorted(catalog.components):
        if len(answers) >= case.failure_count:
            break
        if component in {c.component for c in selected}:
            continue
        info = catalog.components[component]
        reason = sorted(NODE_REASONS if info.kind == "node" else CONTAINER_REASONS)[0]
        answers.append(Answer(case.start if "datetime" in case.requested_fields else None,
                              component if "component" in case.requested_fields else None,
                              reason if "reason" in case.requested_fields else None))
        limitations.append("Insufficient independent episodes: observed catalog component selected without causal evidence")
    while len(answers) < case.failure_count:
        answers.append(Answer(case.start if "datetime" in case.requested_fields else None,
                              "unknown-component" if "component" in case.requested_fields else None,
                              sorted(CONTAINER_REASONS)[0] if "reason" in case.requested_fields else None))
        limitations.append("No further observed component: unknown-component is an unverified placeholder guess")
    answers.sort(key=lambda a: (a.datetime or case.start, a.component or "", a.reason or ""))
    selected_ids = {c.candidate_id for c in selected}
    alternatives = [Alternative(r.candidate.candidate_id, "unresolved",
                    list(r.candidate.supporting_ids),
                    "Lower bounded ranking score; observations do not exclude this explanation")
                    for r in ranked[:12] if r.candidate.candidate_id not in selected_ids]
    limitations.append("Ranking scores and margins are not calibrated probabilities")
    if case.failure_count > 1:
        limitations.append("Episode independence and ordering may be unresolved at telemetry sampling resolution")
    return Decision(answers=answers, selected_candidate_ids=[c.candidate_id for c in selected],
                    confidence=confidence, supporting_ids=sorted({i for c in selected for i in c.supporting_ids}),
                    alternatives=alternatives, limitations=list(dict.fromkeys(limitations)), stop_reason=stop_reason)
