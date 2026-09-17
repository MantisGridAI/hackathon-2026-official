"""Instruction parsing and per-run state, without model or answer-file access."""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
from pathlib import Path
import re
import time

from .contracts import CaseContext, CaseParseError, RunConfig, RunState, UTC8

_MONTHS = {m.lower(): i for i, m in enumerate(("January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"), 1)}
_DATE = re.compile(r"(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+(?P<day>\d{1,2}),?\s+(?P<year>\d{4})|(?P<iso>\d{4}-\d{2}-\d{2})", re.I)
_CLOCK = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)")


def _date(match):
    if match.group("iso"):
        return datetime.fromisoformat(match.group("iso")).replace(tzinfo=UTC8)
    return datetime(int(match.group("year")), _MONTHS[match.group("month").lower()],
                    int(match.group("day")), tzinfo=UTC8)


def parse_case(instruction: str, *, reference_minutes: int = 10) -> CaseContext:
    if not isinstance(instruction, str) or not instruction.strip():
        raise CaseParseError("Instruction is empty")
    if reference_minutes <= 0:
        raise ValueError("reference_minutes must be positive")
    dates, clocks = list(_DATE.finditer(instruction)), list(_CLOCK.finditer(instruction))
    if not dates or len(clocks) < 2:
        raise CaseParseError("Cannot parse two clock bounds and a date")
    warnings = []
    try:
        bounds = []
        for clock in clocks[:2]:
            preceding = [d for d in dates if d.start() < clock.start()]
            if not preceding:
                raise ValueError("Date must precede clock")
            h, m, s = (int(x or 0) for x in clock.groups())
            bounds.append(_date(preceding[-1]).replace(hour=h, minute=m, second=s))
        start, end = bounds
        if end <= start:
            explicit_end = any(clocks[0].end() < d.start() < clocks[1].start() for d in dates)
            if explicit_end:
                raise ValueError("Explicit end is not after start")
            end += timedelta(days=1)
        if end - start > timedelta(days=2):
            raise ValueError("Window exceeds the bounded supported range")
    except ValueError as exc:
        raise CaseParseError("Invalid time window: " + str(exc)) from exc
    if len(clocks) > 2:
        warnings.append("Additional clock times ignored; using first window")
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "a single": 1, "a": 1}
    count = re.search(r"\b(\d+|a single|one|two|three|four|five|six|seven|eight|nine|ten|a)\s+failures?\b", instruction, re.I)
    n = (int(count[1]) if count[1].isdigit() else words[count[1].lower()]) if count else 1
    if n < 1 or n > 20:
        raise CaseParseError("Failure count outside supported 1..20 range")
    if not count:
        warnings.append("Failure count not explicit; best guess is one")
    # Requested output is stated in the task clause. Window times are not requested fields.
    task = re.split(r"you are tasked with|you need to|your task is|please identify|identify(?:ing)?\s+(?:the\s+)?root cause", instruction, flags=re.I)[-1]
    task = task.lower()
    requested = tuple(f for f, pattern in (("datetime", r"datetime|occurrence\s+time|\btime\b|timestamp|when"),
                      ("component", r"component"), ("reason", r"reason|underlying cause")) if re.search(pattern, task))
    if not requested or task == instruction.lower():
        # Read root-cause field phrases only, never infer time from the window.
        requested = tuple(f for f, pattern in (("datetime", r"root cause (?:occurrence )?(?:datetime|time)"),
                          ("component", r"root cause component"), ("reason", r"root cause reason"))
                          if re.search(pattern, instruction, re.I))
        if not requested:
            requested = ("datetime", "component", "reason")
            warnings.append("Requested fields ambiguous; using all three")
    return CaseContext(hashlib.sha256(instruction.encode("utf-8")).hexdigest()[:16], instruction,
                       start, end, start - timedelta(minutes=reference_minutes), n, requested,
                       "degraded" if warnings else "ok", warnings)


def get_run_state(dataset_dir: Path, ctx: dict, config: RunConfig) -> RunState:
    from .data_access import CSVTelemetryStore
    dataset, out = Path(dataset_dir).resolve(), Path(ctx["out_dir"]).resolve()
    existing = ctx.get("rca_state")
    if existing is not None and (existing.store.dataset_dir != dataset or existing.out_dir != out or existing.config != config):
        raise ValueError("RunState cannot be reused across dataset, output or configuration")
    if existing is None:
        started = float(ctx.get("started_monotonic", time.monotonic()))
        out.mkdir(parents=True, exist_ok=True)
        existing = RunState(CSVTelemetryStore(dataset, out_dir=out), config, out, started,
                            started + config.run_soft_seconds)
        ctx["rca_state"] = existing
    existing.invocation_index += 1
    return existing
