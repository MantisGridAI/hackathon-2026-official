"""The single finite RCA investigation controller; no raw CSV or label access."""
from __future__ import annotations

from copy import deepcopy
import time

from .contracts import AnalysisBundle, InvestigationResult
from .prompts import build_messages, validate_selection
from .ranking import (choose_candidates, deterministic_gate, make_decision,
                      merge_bundles, prepare_candidates, rank_candidates)
from .routing import Router, RENDER_RESERVE_S, MODEL_RESERVE_S, snapshot_usage, usage_delta


def _bypass_limitations(events):
    """Expose operational route failures in human evidence, with bounded detail."""
    descriptions = {
        "deadline_exhausted": "the remaining model deadline was exhausted",
        "http_attempt_limit": "the maximum number of HTTP attempts was reached",
        "cost_reservation_limit": "the next request could not fit within the remaining cost budget",
        "model_stage_limit": "the configured model stage limit was reached",
        "prompt_size_limit": "the prompt exceeded the fixed byte budget",
    }
    notes = []
    for event in events:
        if event.get("event") != "bypass":
            continue
        reason = str(event.get("reason", ""))
        if reason.startswith("client_unavailable:"):
            description = "model client initialization was unavailable (" + reason.split(":", 1)[1][:80] + ")"
        elif reason.startswith("circuit_open:"):
            description = "the model circuit breaker was open for " + reason.split(":", 1)[1][:80]
        else:
            description = descriptions.get(reason)
        if description:
            note = "Model routing bypassed because " + description + "; no request was sent for this bypass."
            if note not in notes:
                notes.append(note)
            if len(notes) >= 8:
                break
    return notes


def _call(module, function, *args, deadline):
    if time.monotonic() >= deadline:
        return AnalysisBundle(module, warnings=[f"{module}: skipped because telemetry deadline was exhausted"])
    try:
        return function(*args, deadline=deadline)
    except Exception as exc:
        # Do not misrepresent programming failure as complete/healthy telemetry.
        return AnalysisBundle(module, warnings=[f"{module}: tool failed ({type(exc).__name__}); coverage unknown"])


def _followup_plan(ranked, edges):
    """One discriminating question, not an unconditional tour of every tool."""
    top = ranked[0].candidate
    components = tuple(dict.fromkeys(r.candidate.component for r in ranked[:4]))
    if top.features.get("metrics.family") == "process" or top.reason == "container process termination":
        return dict(tool="search_fault_evidence", question="Do service logs show termination, OOM or restart evidence?",
                    components=components, patterns=("oom", "out of memory", "killed", "terminated", "restart", "exit"))
    if "metrics.family" in top.features and not top.features.get("m4.weak_reason"):
        return dict(tool="compare_replicas_and_node", question="Is the resource change isolated to this component or shared with its replicas/node?",
                    components=(top.component,))
    if edges:
        return dict(tool="inspect_dependencies", question="Which observed dependency edges support a local fault versus propagated waiting?",
                    components=components)
    return dict(tool="search_fault_evidence", question="Do service logs distinguish transport errors from resource/process symptoms?",
                components=components, patterns=("error", "timeout", "reset", "refused", "retry", "oom", "killed"))


def _escalation_reason(reply, picks, ranked, evidence):
    """Escalate unresolved competition, never ask a model to repair missing data."""
    if all(c.features.get("m4.weak_reason") for c in picks):
        return None
    if any(c.contradicting_ids for c in picks):
        return "conflicting_observations"
    if reply["confidence"] != "low" or not reply["unresolved"]:
        return None
    by_id = {e.evidence_id: e for e in evidence}
    def measured(candidate):
        records = [by_id[key] for key in candidate.supporting_ids if key in by_id]
        return bool(records) and all(record.coverage and all(c.status == "complete" for c in record.coverage) for record in records)
    scores = {item.candidate.candidate_id: item.score for item in ranked}
    for pick in picks:
        if not measured(pick):
            continue
        for alternative in ranked:
            other = alternative.candidate
            if (other.component, other.reason) == (pick.component, pick.reason):
                continue
            if other.features.get("m4.weak_reason") or not measured(other):
                continue
            if abs(alternative.score - scores.get(pick.candidate_id, alternative.score)) <= .75:
                return "unresolved_competing_observations"
    return None


def investigate(case, state, *, deadline: float) -> InvestigationResult:
    from .metrics import triage_metrics, compare_replicas_and_node
    from .traces import triage_traces, inspect_dependencies
    from .logs import search_fault_evidence

    router = Router(case, state, deadline=deadline)
    before = snapshot_usage(state)

    def timed_tool(name, module, function, *args, deadline):
        started = time.monotonic()
        bundle = _call(module, function, *args, deadline=deadline)
        interruptions = [warning for warning in bundle.warnings
                         if any(word in warning.lower() for word in ("deadline", "row cap", "row/deadline", "tool failed", "skipped", "memory limit"))]
        interruptions.extend(f'{coverage.source}: {coverage.status}' for coverage in bundle.coverage
                             if coverage.status in ('partial', 'failed', 'not_queried'))
        router._record(dict(event="tool", stage="telemetry", tool=name,
                            status="incomplete" if interruptions else "complete", interruptions=interruptions,
                            latency_s=round(time.monotonic() - started, 6),
                            candidate_count=len(bundle.candidates), evidence_count=len(bundle.evidence),
                            coverage=[dict(source=c.source, status=c.status,
                                           rows_scanned=c.rows_scanned, rows_matched=c.rows_matched)
                                      for c in bundle.coverage]))
        return bundle
    # Share telemetry time between both independent triages; metrics cannot consume
    # the whole case budget and prevent trace-only faults from being considered.
    usable = max(0., min(deadline, state.deadline) - time.monotonic() - RENDER_RESERVE_S)
    model_reserve = MODEL_RESERVE_S if state.config.mode != "deterministic" and state.config.max_model_stages > 0 else 0.
    telemetry_end = min(deadline, state.deadline) - RENDER_RESERVE_S - model_reserve
    telemetry_seconds = max(0., telemetry_end - time.monotonic())
    metric_deadline = time.monotonic() + telemetry_seconds * .47
    trace_deadline = telemetry_end
    bundles = [timed_tool("triage_metrics", "m2", triage_metrics, case, state.store, deadline=metric_deadline),
               timed_tool("triage_traces", "m3", triage_traces, case, state.store, deadline=trace_deadline)]
    candidates, evidence, edges, warnings = merge_bundles(bundles)
    catalog = state.store.component_catalog()
    prepared = prepare_candidates(case, candidates, catalog, evidence)
    ranked = rank_candidates(case, prepared, evidence)
    selected = choose_candidates(ranked, case.failure_count, edges)
    fallback = make_decision(case, selected, ranked, catalog,
                             warnings=warnings + case.parse_warnings)

    if (ranked and state.config.max_followups > 0 and
            not deterministic_gate(case, ranked, selected, evidence) and
            time.monotonic() < telemetry_end - 3.):
        plan = _followup_plan(ranked, edges)
        router._record(dict(event="followup_plan", stage="investigation", status="planned", **plan))
        followup_deadline = min(telemetry_end,
                                time.monotonic() + min(5., usable * .12))
        if plan["tool"] == "compare_replicas_and_node":
            extra = timed_tool("compare_replicas_and_node", "m2", compare_replicas_and_node, case, plan["components"][0],
                          state.store, deadline=followup_deadline)
        elif plan["tool"] == "inspect_dependencies":
            extra = timed_tool("inspect_dependencies", "m3", inspect_dependencies, case, plan["components"],
                          tuple(edges), state.store, deadline=followup_deadline)
        else:
            extra = timed_tool("search_fault_evidence", "m3", search_fault_evidence, case, plan["components"],
                               plan["patterns"], ("log_service",), state.store, deadline=followup_deadline)
        # Any identical stable ID with changed content is an integration error.
        candidates, evidence, edges, warnings = merge_bundles(bundles + [extra])
        catalog = state.store.component_catalog()
        prepared = prepare_candidates(case, candidates, catalog, evidence)
        ranked = rank_candidates(case, prepared, evidence)
        selected = choose_candidates(ranked, case.failure_count, edges)
        fallback = make_decision(case, selected, ranked, catalog,
                                 warnings=warnings + case.parse_warnings)
    else:
        why = "no_candidates" if not ranked else "configured_off" if state.config.max_followups <= 0 else "sufficient_evidence" if deterministic_gate(case, ranked, selected, evidence) else "insufficient_followup_time"
        router._record(dict(event="followup_plan", stage="investigation", status="skipped", reason=why))
    decision = deepcopy(fallback)
    gate = deterministic_gate(case, ranked, selected, evidence)
    if state.config.mode == "deterministic" or gate or not ranked or state.config.max_model_stages <= 0:
        why = ("deterministic_mode" if state.config.mode == "deterministic" else
               "deterministic_gate" if gate else "no_supported_candidates" if not ranked else "model_stage_limit")
        router.bypass(why)
        decision.stop_reason = why
        decision.confidence = "medium" if gate else "low"
    else:
        for stage in ("flash", "strong")[:max(0, min(2, state.config.max_model_stages))]:
            messages, offered, records = build_messages(case, ranked, evidence, stage=stage)
            if len(choose_candidates([r for r in ranked if r.candidate in offered], case.failure_count, edges)) < case.failure_count:
                router.bypass("insufficient_supported_episodes", stage)
                break
            reply = router.request(stage, messages, reason="ambiguous_telemetry",
                validate=lambda text: validate_selection(text, case, offered, records))
            if reply is None:
                continue
            by_id = {c.candidate_id: c for c in offered}
            picks = [by_id[i] for i in reply["selected_candidate_ids"]]
            # Model confidence is advisory: factual limitations cap it.
            unresolved = reply["unresolved"] + [s for c in picks for s in c.unresolved]
            selected_evidence = [e for e in evidence if e.evidence_id in {i for c in picks for i in c.supporting_ids}]
            limited_coverage = any(not e.coverage or any(cv.status != "complete" for cv in e.coverage)
                                   for e in selected_evidence)
            confidence = "low" if reply["confidence"] == "low" or unresolved or limited_coverage or any(c.contradicting_ids for c in picks) else "medium"
            decision = make_decision(case, picks, ranked, catalog,
                warnings=warnings + case.parse_warnings + ["Model advice (not a measured fact): " + s for s in reply["unresolved"]],
                confidence=confidence, stop_reason=stage + "_selection")
            router._record(dict(event="selection", stage=stage, status="applied",
                                selection_applied=True,
                                selected_candidate_ids=reply["selected_candidate_ids"],
                                supporting_evidence_ids=reply["supporting_evidence_ids"],
                                confidence=confidence))
            if stage == "strong":
                break
            # An undistinguishable mechanism cannot be repaired by another model.
            if all(c.features.get("m4.weak_reason") for c in picks):
                decision.limitations.append("No mechanism-specific evidence to resolve the subtype; strong escalation bypassed")
                router.bypass("no_new_mechanism_evidence", "strong")
                break
            escalation = _escalation_reason(reply, picks, ranked, evidence)
            if escalation is None:
                router.bypass("no_discriminable_model_question", "strong")
                break
            router._record(dict(event="escalation", stage="strong", status="planned", reason=escalation))
    for result in (decision, fallback):
        result.route_events = deepcopy(router.events)
        result.usage_delta = usage_delta(state, before)
        result.limitations.extend(_bypass_limitations(router.events))
        failed = [e for e in router.events if e["event"] == "request" and e["status"] != "valid"]
        if failed:
            result.limitations.append(f"{len(failed)} model attempt(s) failed response or transport validation; failed advice was not used")
        if any(e.get("event") == "request" and e.get("estimated_cost_usd") is None for e in router.events):
            result.limitations.append("Provider usage missing on some attempts; unknown costs retain conservative budget reservations")
    return InvestigationResult(decision, fallback, evidence)
