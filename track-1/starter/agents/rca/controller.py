"""The single finite RCA investigation controller; no raw CSV or label access."""
from __future__ import annotations

from copy import deepcopy
import time

from .contracts import AnalysisBundle, InvestigationResult
from .prompts import build_messages, validate_selection
from .ranking import (choose_candidates, deterministic_gate, make_decision,
                      merge_bundles, prepare_candidates, rank_candidates)
from .routing import Router, RENDER_RESERVE_S, snapshot_usage, usage_delta


def _bypass_limitations(events):
    """Expose operational route failures in human evidence, with bounded detail."""
    descriptions = {
        "deadline_exhausted": "the remaining model deadline was exhausted",
        "http_attempt_limit": "the maximum number of HTTP attempts was reached",
        "cost_reservation_limit": "the next request could not fit within the remaining cost budget",
        "model_stage_limit": "the configured model stage limit was reached",
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


def investigate(case, state, *, deadline: float) -> InvestigationResult:
    from .metrics import triage_metrics, compare_replicas_and_node
    from .traces import triage_traces, inspect_dependencies

    router = Router(case, state, deadline=deadline)
    before = snapshot_usage(state)
    # Share telemetry time between both independent triages; metrics cannot consume
    # the whole case budget and prevent trace-only faults from being considered.
    usable = max(0., min(deadline, state.deadline) - time.monotonic() - RENDER_RESERVE_S)
    metric_deadline = min(deadline - RENDER_RESERVE_S, time.monotonic() + usable * .32)
    trace_deadline = min(deadline - RENDER_RESERVE_S, time.monotonic() + usable * .68)
    bundles = [_call("m2", triage_metrics, case, state.store, deadline=metric_deadline),
               _call("m3", triage_traces, case, state.store, deadline=trace_deadline)]
    candidates, evidence, edges, warnings = merge_bundles(bundles)
    catalog = state.store.component_catalog()
    prepared = prepare_candidates(case, candidates, catalog, evidence)
    ranked = rank_candidates(case, prepared, evidence)
    selected = choose_candidates(ranked, case.failure_count, edges)
    fallback = make_decision(case, selected, ranked, catalog,
                             warnings=warnings + case.parse_warnings)

    if (ranked and state.config.max_followups > 0 and
            not deterministic_gate(case, ranked, selected, evidence) and
            time.monotonic() < deadline - RENDER_RESERVE_S - 3.):
        top = ranked[0].candidate
        followup_deadline = min(deadline - RENDER_RESERVE_S,
                                time.monotonic() + min(5., usable * .12))
        if "metrics.family" in top.features:
            extra = _call("m2", compare_replicas_and_node, case, top.component,
                          state.store, deadline=followup_deadline)
        else:
            components = tuple(dict.fromkeys(r.candidate.component for r in ranked[:4]))
            extra = _call("m3", inspect_dependencies, case, components,
                          tuple(edges), state.store, deadline=followup_deadline)
        # Any identical stable ID with changed content is an integration error.
        candidates, evidence, edges, warnings = merge_bundles(bundles + [extra])
        catalog = state.store.component_catalog()
        prepared = prepare_candidates(case, candidates, catalog, evidence)
        ranked = rank_candidates(case, prepared, evidence)
        selected = choose_candidates(ranked, case.failure_count, edges)
        fallback = make_decision(case, selected, ranked, catalog,
                                 warnings=warnings + case.parse_warnings)
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
            confidence = "low" if unresolved or limited_coverage or any(c.contradicting_ids for c in picks) else "medium"
            decision = make_decision(case, picks, ranked, catalog,
                warnings=warnings + case.parse_warnings + ["Model advice (not a measured fact): " + s for s in reply["unresolved"]],
                confidence=confidence, stop_reason=stage + "_selection")
            if confidence == "medium" or stage == "strong":
                break
            # An undistinguishable mechanism cannot be repaired by another model.
            if all(c.features.get("m4.weak_reason") for c in picks):
                decision.limitations.append("No mechanism-specific evidence to resolve the subtype; strong escalation bypassed")
                router.bypass("no_new_mechanism_evidence", "strong")
                break
    for result in (decision, fallback):
        result.route_events = deepcopy(router.events)
        result.usage_delta = usage_delta(state, before)
        result.limitations.extend(_bypass_limitations(router.events))
        failed = [e for e in router.events if e["event"] == "request" and e["status"] != "valid"]
        if failed:
            result.limitations.append(f"{len(failed)} model attempt(s) failed response or transport validation; retained best guess")
        if any(e.get("event") == "request" and e.get("estimated_cost_usd") is None for e in router.events):
            result.limitations.append("Provider usage missing on some attempts; unknown costs retain conservative budget reservations")
    return InvestigationResult(decision, fallback, evidence)
