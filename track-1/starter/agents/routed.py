"""Official RCA entry point: bounded tools, model routing and validated evidence."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import time

from run import Solution, format_prediction
from agents.rca.contracts import CONTAINER_REASONS, ComponentCatalog
from agents.rca.ranking import make_decision
from agents.rca.routing import load_config, snapshot_usage, usage_delta
from agents.rca.runtime import get_run_state, parse_case


def _emergency(instruction, *, case=None, decision=None, notes=(), usage=None):
    """Shape-preserving failure output, with no claims of observed evidence."""
    if case is not None:
        fields, count = case.requested_fields, case.failure_count
    else:
        task = re.split(r"you are tasked with|your task is|please identify", str(instruction), flags=re.I)[-1]
        fields = tuple(key for key, pattern in (("datetime", r"datetime|occurrence time|timestamp"),
                       ("component", "component"), ("reason", "reason"))
                       if re.search(pattern, task, re.I)) or ("datetime", "component", "reason")
        words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                 "seven": 7, "eight": 8, "nine": 9, "ten": 10}
        match = re.search(r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+failures?\b", str(instruction), re.I)
        count = (int(match[1]) if match[1].isdigit() else words[match[1].lower()]) if match else 1
        count = min(20, max(1, count))
    answers = []
    for index in range(count):
        answer = decision.answers[index] if decision and index < len(decision.answers) else None
        item = {}
        for field in fields:
            value = getattr(answer, field, None)
            if field == "datetime":
                value = value.strftime("%Y-%m-%d %H:%M:%S") if isinstance(value, datetime) else (
                    case.start.strftime("%Y-%m-%d %H:%M:%S") if case else "1970-01-01 00:00:00")
            elif field == "component":
                value = value or "unknown-component"
            else:
                value = value or sorted(CONTAINER_REASONS)[0]
            item[field] = value
        answers.append(item)
    prediction = format_prediction(answers)
    text = ("## Answer\n\n" + prediction + "\n\n## Confidence\n\nLow. Emergency best guess; "
            "output shape is preserved but semantic validity is unverified.\n\n## Evidence\n\n"
            "No measured facts are claimed by this emergency path. "
            + ("Instruction window could not be reliably parsed; any epoch time is an explicit placeholder guess. " if case is None else "")
            + "\n" + "\n".join("- " + str(note) for note in notes)
            + "\n\n## Ruled out\n\nNo alternative was ruled out; missing or failed checks do not establish health.\n")
    return Solution(prediction, text, usage or {})


def solve(instruction: str, dataset_dir: Path, ctx: dict) -> Solution:
    state = None
    case = None
    fallback = None
    before = {}
    started = time.monotonic()
    try:
        config = load_config()
        state = get_run_state(dataset_dir, ctx, config)
        before = snapshot_usage(state)
        case = parse_case(instruction, reference_minutes=config.reference_minutes)
        # Preserve a legal-count fallback before invoking any module/model.
        fallback = make_decision(case, [], [], state.store.component_catalog(),
                                 warnings=["No causal evidence collected yet"])
        from agents.rca.controller import investigate
        result = investigate(case, state, deadline=min(state.deadline, started + config.case_soft_seconds))
        fallback = result.fallback
        from agents.rca.validation import validate_and_render
        result.decision.usage_delta = usage_delta(state, before)
        fallback.usage_delta = usage_delta(state, before)
        rendered = validate_and_render(case, result.decision, result.evidence,
                                       state.store.component_catalog())
        if rendered.validation_status == "invalid":
            fallback.limitations.extend(["Preferred decision rejected by output validation"] + rendered.errors)
            rendered = validate_and_render(case, fallback, result.evidence,
                                           state.store.component_catalog())
        if rendered.validation_status == "invalid":
            return _emergency(instruction, case=case, decision=fallback,
                              notes=rendered.errors + rendered.warnings,
                              usage=usage_delta(state, before))
        evidence = rendered.evidence
        # Warnings must survive even if a renderer omits them from its prose.
        if rendered.warnings:
            evidence += "\n" + "\n".join("- Validation: " + str(w) for w in rendered.warnings) + "\n"
        return Solution(rendered.prediction, evidence, usage_delta(state, before))
    except Exception as exc:
        # Include paid attempts even when downstream formatting or tooling failed.
        counts = usage_delta(state, before) if state is not None else {}
        return _emergency(instruction, case=case, decision=fallback,
                          notes=["Runtime failure: " + type(exc).__name__], usage=counts)
