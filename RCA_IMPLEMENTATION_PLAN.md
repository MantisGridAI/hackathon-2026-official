# Hybrid RCA Agent Implementation Plan

## Direction

Build a hybrid root-cause analysis agent rather than copying OpenRCA or
ADS-KGRCA end to end:

```text
deterministic telemetry analysis
        -> trace-aware candidate ranking
        -> cheap GLM only when ambiguous
        -> strong GLM only for genuinely hard cases
        -> deterministic validation and evidence
```

The agent should imitate an SRE investigation:

1. Check overall service health.
2. Diagnose suspicious containers or nodes.
3. Trace the failure upstream and downstream.
4. Search only the relevant logs and telemetry.
5. Produce a constrained root-cause answer with measured evidence.

The highest-value work is metric onset detection, trace causality, candidate
ranking, confidence-gated routing, and deterministic evidence generation.

## Scope and Interface

Keep the official starter as the backbone. Modify only
`track-1/starter/agents/routed.py` unless a narrowly scoped supporting change is
required. Preserve the `solve(instruction, dataset_dir, ctx) -> Solution`
contract, `run.py`, usage accounting, prediction formatting, and failure
handling.

Every case must emit:

- Exactly the number of failures stated in the question.
- Exact component names and one of the 15 legal reason strings.
- A timestamp in the requested incident window, formatted through
  `format_prediction()`.
- Evidence that uses observed facts and states uncertainty honestly.

Do not add Harzoo, MCP, Phoenix, Parquet preprocessing, or a second agent
framework to the critical runtime path. Plain Python helpers are sufficient for
the hackathon.

## 1. Preserve the Starter and Submission Contract

- Keep `run.py` unchanged.
- Keep the current routed agent entry point.
- Parse the date, time range, failure count, and task fields deterministically.
- Always return a best guess, even when every model is unavailable.
- Validate model output against the current candidate set and legal reason set.
- Fall back to the deterministic answer on malformed output or API failure.

Formatting mistakes can zero an otherwise correct case, so contract validation
comes before any model improvement.

## 2. Progressive Telemetry Narrowing

Do not build a full production preprocessing system first. Narrow the search
space as evidence accumulates:

```text
small service metrics
        -> candidate shortlist
        -> incident-window container/node metrics
        -> traces and mesh edges
        -> targeted logs
```

Use proper CSV readers and respect the dataset traps:

- Metrics and logs use seconds; traces use milliseconds.
- Displayed answer times are UTC+8.
- Container IDs encode node and service relationships.
- Mesh IDs encode source and destination in their names.
- Missing data is not automatically zero.
- Large trace and proxy files must be filtered to the incident window before
  expensive analysis.

Cache only lightweight per-day or per-case data that materially reduces repeat
scans. Avoid building indexes whose cost exceeds the judging time budget.

## 3. Six Core Analysis Steps

### 3.1 Service triage and onset

Start with `metric_service.csv` and compare the incident window with nearby
baseline data. Inspect success rate, response time, request rate, and count.

For each affected service, record:

```text
component
anomaly type
first sustained change
persistence
baseline contrast
replica contrast
```

Prefer the first sustained change over the largest spike. This is the first
layer of the health-check design and supplies the shortlist.

### 3.2 Container and node diagnosis

Inspect detailed metrics only for shortlisted services and their neighbors.
Prioritize CPU, memory, read I/O, write I/O, and process termination signals.

Compare suspect containers with sibling replicas. Prefer a node-level cause
when several unrelated containers on the node change together and the node
changes first. Prefer a container-level cause when one container changes first
and explains the later aggregate signal.

### 3.3 Trace and topology analysis

Use `trace_span.csv` in the incident window to reconstruct a small causal graph:

- Group spans by `trace_id`.
- Join `parent_span` to `span_id`.
- Track component, duration, status, and operation.
- Compare parent duration with child duration.
- Identify where abnormal latency or errors first appear.
- Penalize components that become abnormal only after an upstream failure.

Use the graph to distinguish a root cause from a downstream symptom. A service
with the largest anomaly is not necessarily the service that failed first.

### 3.4 Targeted network and log inspection

Only inspect mesh and proxy data for candidate components and edges. Look for
latency gaps, retries, resets, refused connections, timeouts, packet loss,
retransmission, and corruption signals.

Search relevant service or proxy logs only after metric and trace narrowing.
Simple patterns are sufficient initially:

```text
ERROR  timeout  connection  reset  refused
OOM    killed   retry       unavailable
```

Require fault-specific evidence before choosing among network reason labels.

### 3.5 Candidate ranking

Represent each candidate with:

- Exact component and legal reason.
- First-change time and persistence.
- Direct supporting evidence.
- Contradicting evidence.
- Healthy replica and node comparisons.
- Trace propagation position.
- Downstream failures explained.
- Confidence.

Rank with a simple interpretable score:

```text
anomaly strength
+ early onset
+ trace support
+ replica contrast
+ node/container consistency
+ cross-telemetry agreement
+ downstream failures explained
- downstream symptom penalty
- contradictory evidence
```

Do not optimize weights before the features are working. For multiple-failure
windows, separate independent propagation chains and return answers in
chronological order instead of selecting the loudest N symptoms.

### 3.6 Deterministic evidence and validation

Generate evidence directly from measured values. Include:

- Final answer and confidence.
- Metric onset and baseline comparison.
- Trace propagation and latency evidence.
- Targeted log or network evidence when present.
- Alternatives considered and why they were ruled out.
- Missing telemetry, model failures, and remaining ambiguity.

Never ask a model to invent measurements or write the authoritative evidence.

## 4. Confidence-Gated GLM Routing

### Level 1: no model

Answer deterministically when the leader has a clear score margin, direct
fault-specific evidence, consistent onset and propagation, and no major
contradictions. This saves both time and cost.

### Level 2: cheap Flash model

For uncertain cases, send a compact candidate summary rather than raw telemetry:

```json
{
  "candidate_1": {
    "component": "payment-0",
    "reason": "container network latency",
    "onset": "2022-03-20 09:08:42",
    "supporting_evidence": ["earliest trace anomaly"],
    "contradictions": ["CPU normal"]
  }
}
```

Require JSON and constrain the answer to supplied candidates and legal reasons.
Use the existing fallback-aware `LLM` wrapper and keep the cheap tier ordered:

```python
CHEAP = ["zai-org/GLM-4.7-Flash", "zai-org/GLM-5.3-Flash"]
```

### Level 3: strong GLM

Escalate only when candidates are close, Flash confidence is low, Flash
disagrees with deterministic ranking, node-versus-container causality is
unclear, network subtype is unresolved, multiple failures overlap, or telemetry
sources conflict.

Provide the strong model with candidate summaries, causal order, supporting and
contradicting evidence, and the Flash decision. Ask for a short constrained JSON
answer:

```python
STRONG = ["zai-org/GLM-5.2", "zai-org/GLM-5.1"]
```

The 15 legal reasons make this a constrained classification problem, not an
open-ended request to explain the incident.

## 5. Reliability and Budget

- Read credentials and endpoint from the environment through `llm.py`.
- Let the existing wrapper retry briefly, fall back within a tier, and stop
  retrying a model after repeated capacity failures.
- Degrade to the deterministic candidate when all model calls fail.
- Keep prompts compact and outputs short.
- Avoid repeated scans of multi-gigabyte files.
- Target substantially less than one minute per case on average.
- Keep cost comfortably below the $1.25 judged-run average per case.

## 6. Evaluation Plan

Compare configurations on the same development cases:

1. Existing metric-only heuristic.
2. Improved deterministic telemetry and trace agent.
3. Flash-only routing.
4. Strong-only routing.
5. Confidence-routed hybrid agent.

At minimum compare the routed agent with:

```bash
make dev N=20 AGENT=agents.routed
RCA_MODEL=zai-org/GLM-5.2 make dev N=20 AGENT=agents.routed
make score
make cost
```

Record strict and partial accuracy, accuracy by task and difficulty, fully
solved cases, dollars per case, runtime, model calls, escalation rate, and
repeat-run variance. Spot-check evidence against raw telemetry. Categorize
errors as timestamp, component, reason, node/container, network, multiple
failure, or formatting errors.

## Definition of Done

- The starter contract remains intact.
- Easy cases can finish without a model.
- Ambiguous cases use Flash before strong escalation.
- Trace timing can distinguish root causes from downstream symptoms.
- Logs and network telemetry are searched only for narrowed candidates.
- Predictions contain the exact failure count, legal labels, and valid times.
- Evidence is deterministic, measured, and explicit about uncertainty.
- Model failures still produce a prediction and evidence file.
- Routed versus single-model accuracy, cost, and runtime are documented.
