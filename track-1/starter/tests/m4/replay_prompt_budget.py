"""Offline prompt-size comparison from saved IDs/support and actual evidence.

This reconstructs the recorded selected/alternative shortlist, not the complete
original prompt: the ledger intentionally omits raw model messages and candidate
feature vectors. No data scan, answer-file read, model call or network is used.
"""
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType

STARTER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(STARTER))
from agents.rca.contracts import Candidate, CaseContext, Coverage, EvidenceRecord, QuerySpec, SourceRecord
from agents.rca.prompts import build_messages, prompt_bytes, MAX_PROMPT_BYTES
from agents.rca.ranking import Ranked


def decode_record(raw):
    fields = dict(raw)
    fields["interval"] = tuple(datetime.fromisoformat(value) for value in fields["interval"])
    coverage = []
    for original in fields["coverage"]:
        item = dict(original)
        for key in ("first_time", "last_time"):
            item[key] = datetime.fromisoformat(item[key]) if item[key] else None
        coverage.append(Coverage(**item))
    fields["coverage"] = coverage
    queries = []
    for original in fields["queries"]:
        item = dict(original)
        for key in ("start", "end"):
            item[key] = datetime.fromisoformat(item[key])
        queries.append(QuerySpec(**item))
    fields["queries"] = queries
    fields["source_records"] = [SourceRecord(**item) for item in fields["source_records"]]
    return EvidenceRecord(**fields)


def replay(path, baseline):
    saved = json.loads(Path(path).read_text(encoding="utf-8"))
    fields = dict(saved["case"])
    for key in ("start", "end", "reference_start"):
        fields[key] = datetime.fromisoformat(fields[key])
    fields["requested_fields"] = tuple(fields["requested_fields"])
    case = CaseContext(**fields)
    records = [decode_record(item) for item in saved["evidence"]]
    by_id = {r.evidence_id: r for r in records}
    choices = {key: saved["decision"]["supporting_ids"] for key in saved["decision"]["selected_candidate_ids"]}
    choices.update({a["candidate_id"]: a["evidence_ids"] for a in saved["decision"]["alternatives"]})
    ranked = []
    for key, references in choices.items():
        observed = next((by_id[e].component_ids[0] for e in references if e in by_id and by_id[e].component_ids), "metadata-not-retained")
        ranked.append(Ranked(Candidate(key, observed, supporting_ids=list(references),
            unresolved=["Offline size replay: original reason/features are not retained in the ledger"]), 0., {}))
    before, before_candidates, before_records = baseline.build_messages(case, ranked, records, stage="flash")
    after, after_candidates, after_records = build_messages(case, ranked, records, stage="flash")
    payload = json.loads(after[1]["content"])
    shown = {r.evidence_id for r in after_records}
    assert prompt_bytes(after) <= MAX_PROMPT_BYTES
    assert {c.candidate_id for c in after_candidates} <= choices.keys()
    assert all(c["supporting_ids"] and set(c["supporting_ids"] + c["contradicting_ids"]) <= shown for c in payload["candidates"])
    assert all(set(c["supporting_ids"]) <= set(choices[c["candidate_id"]]) for c in payload["candidates"])
    return {"case_key": case.case_key, "ledger_records": len(records),
        "reconstructed_candidate_count": len(ranked), "before_bytes": prompt_bytes(before),
        "before_offered_candidates": len(before_candidates), "before_evidence_records": len(before_records),
        "after_bytes": prompt_bytes(after), "after_offered_candidates": len(after_candidates),
        "after_evidence_records": len(after_records), "byte_limit": MAX_PROMPT_BYTES,
        "original_ids_and_support_preserved": True,
        "scope": "Recorded selected/alternative IDs and support only; original features/reasons/model messages were not retained"}


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("Usage: python tests/m4/replay_prompt_budget.py BASELINE_GIT_REV LEDGER_JSON [...]")
    repository = STARTER.parents[1]
    source = subprocess.check_output(["git", "show", sys.argv[1] + ":track-1/starter/agents/rca/prompts.py"], cwd=repository, text=True)
    baseline = ModuleType("baseline_prompts")
    exec(compile(source, "baseline_prompts.py", "exec"), baseline.__dict__)
    print(json.dumps([replay(path, baseline) for path in sys.argv[2:]], indent=2))
