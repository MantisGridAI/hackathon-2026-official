# Hybrid RCA Agent Implementation Plan

## Five-person implementation entry point

Use [TEAM.md](TEAM.md) to claim work. The five [module specifications](TEAM.md#1-认领一块完整交付不认领一个模糊主题),
[rca-v1 shared interfaces](docs/INTERFACES.md), and [integration gates](docs/INTEGRATION.md)
turn this strategy into concrete inputs, outputs, file ownership and acceptance checks.
The strategy below is retained; the five-module specifications govern team boundaries.
These documents describe implementation work, not a completed replacement Agent.

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

Use the official starter/OpenRCA approach as the backbone: query and compress
telemetry before model reasoning. Borrow anomaly detection and topology-aware
ranking ideas from ADS-KGRCA without reproducing its full research pipeline.

The agent should imitate an SRE investigation:

1. Check overall service health.
2. Diagnose suspicious containers or nodes.
3. Trace the failure upstream and downstream.
4. Search only the relevant logs and telemetry.
5. Produce a constrained root-cause answer with measured evidence.

The highest-value work is metric onset detection, trace causality, candidate
ranking, confidence-gated routing, and deterministic evidence generation.

## Scope and Interface

Keep the official starter as the backbone. Evolve
`track-1/starter/agents/routed.py` and use small supporting Python modules under
`track-1/starter/agents/` where they simplify integration. Preserve the `solve(instruction, dataset_dir, ctx) -> Solution`
contract, `run.py`, usage accounting, prediction formatting, and failure
handling.

Every case must emit the fields requested by its instruction:

- Exactly the number of failures stated in the question.
- Exact component names and one of the 15 legal reason strings when requested.
- A timestamp, when requested, in the incident window, formatted through
  `format_prediction()`.
- Evidence that uses observed facts and states uncertainty honestly.

Do not add Harzoo, MCP, Phoenix, Parquet preprocessing, or a second agent
framework to the critical runtime path. Plain Python helpers are sufficient for
the hackathon.

## Six Core Implementation Steps

### 1. Preserve the Starter and Submission Contract

- Preserve the runner logic in `run.py`. At submission time, set its default
  agent to the finished module as required by the starter; use `--agent` during
  development.
- Keep the current routed agent entry point.
- Parse the date, time range, failure count, and task fields deterministically.
- Always return a best guess, even when every model is unavailable.
- Validate model output against the current candidate set and legal reason set.
- Fall back to the deterministic answer on malformed output or API failure.

Formatting mistakes can zero an otherwise correct case, so contract validation
comes before any model improvement.

### 2. Generate Candidates with Metrics

#### Progressive telemetry narrowing

Do not build a full production preprocessing system first. Narrow the search
space as evidence accumulates:

```text
small service metrics
        + broad, cheap container/node scan
        + independent incident-window trace triage
        -> combined candidate shortlist
        -> detailed container/node comparisons and trace/mesh edges
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

#### Service triage and onset

Start with `metric_service.csv` and compare the incident window with nearby
pre-incident baseline data. Inspect success rate, response time, request rate,
and count. Use post-incident data, when available, to check recovery rather than
treating it automatically as a healthy baseline. Start with a robust score based
on median and median absolute deviation, guarding against zero spread and
missing samples; record sustained threshold crossings and persistence.

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

#### Container and node diagnosis

Use service summaries to prioritize, not to exclude components. A cheap broad
container/node scan and independent trace triage must retain faults that do not
move service aggregates. Then inspect detailed metrics for the combined
shortlist and its neighbors.
Prioritize CPU, memory, read I/O, write I/O, and process termination signals.

Compare suspect containers with sibling replicas. Prefer a node-level cause
when several unrelated containers on the node change together and the node
changes first. Prefer a container-level cause when one container changes first
and explains the later aggregate signal.

### 3. Reconstruct Trace Causality and Gather Targeted Logs

Use `trace_span.csv` around the incident window, with a bounded baseline for
comparison, to reconstruct a small dependency graph:

- Group spans by `trace_id`.
- Join `parent_span` to `span_id`.
- Track component, duration, status, and operation.
- Compare parent duration with child duration.
- Identify where abnormal latency or errors first appear.
- Penalize later symptoms only when timing and dependency evidence support
  propagation from another candidate.

Use the graph to distinguish a root cause from a downstream symptom. A service
with the largest anomaly is not necessarily the service that failed first.
Keep call direction separate from fault propagation: in
`frontend -> checkout -> payment`, payment may fail first and cause later
checkout and frontend degradation, propagating back toward callers. Earlier
onset supports a hypothesis; it does not prove causality by itself. Missing
parents, sparse samples, or uncertain timing must reduce confidence.

#### Targeted network and log inspection

Only inspect mesh and proxy data for candidate components and edges. Look for
latency gaps, retries, resets, refused connections, timeouts, packet loss,
retransmission, and corruption signals.

Search relevant service or proxy logs only after metric and trace narrowing.
Simple patterns are sufficient initially:

```text
ERROR  timeout  connection  reset  refused
OOM    killed   retry       unavailable
```

Require fault-specific evidence before claiming that network subtypes have been
distinguished. If the evidence cannot distinguish them at the stopping point,
still make a best legal prediction and state the unresolved subtype in evidence.

### 4. Rank Candidates with Simple Causal Features

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

### 5. Add Confidence-Gated GLM Routing

#### Level 1: no model

Answer deterministically when the leader has a clear score margin, direct
fault-specific evidence, consistent onset and propagation, and no major
contradictions. This saves both time and cost.

#### Level 2: cheap Flash model

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

#### Level 3: strong GLM

Escalate only when candidates are close, Flash confidence is low, Flash
disagrees with deterministic ranking, node-versus-container causality is
unclear, network subtype is unresolved, multiple failures overlap, or telemetry
sources conflict, or Flash returns invalid output. Allow a bounded strong-tier
attempt after a Flash failure when budget remains; otherwise use the
deterministic fallback.

Provide the strong model with candidate summaries, causal order, supporting and
contradicting evidence, and the Flash decision. Ask for a short constrained JSON
answer:

```python
STRONG = ["zai-org/GLM-5.2", "zai-org/GLM-5.1"]
```

The 15 legal reasons make this a constrained classification problem, not an
open-ended request to explain the incident. Source the exact labels from the
starter and validate node/container compatibility. CPU or memory measurements
can support their corresponding load labels; several unrelated containers
degrading together can support a node hypothesis. Generic timeouts alone cannot
distinguish network latency, packet loss, retransmission, and corruption.

Keep candidate scores separate from calibrated probabilities. Model-reported
confidence alone must not control escalation; use score margins, missing
evidence, contradictions, and validation results as well.

### 6. Generate Deterministic Evidence and Evaluate

Generate evidence directly from measured values. Include:

- Final answer and confidence, with the routing decision.
- Metric onset, baseline comparison, and persistence.
- Trace propagation, latency evidence, and healthy replica comparisons.
- Targeted log or network evidence when present.
- Alternatives considered and the measurements that weaken them.
- Missing telemetry, model failures, and remaining ambiguity.

Attach source references and time windows to measurements. Never ask a model to
invent measurements or write the authoritative evidence. Unobserved CPU or
memory is missing evidence, not proof that those resources were normal.

Validate exact failure count, component/reason compatibility, timestamps, and
`format_prediction()` output before returning `Solution`. For multiple failures,
select independent supported hypotheses rather than duplicate symptoms. If the
shortlist is insufficient, widen the bounded search and make the best legal
fallback guesses, marking weak evidence honestly. Verify the runner writes a
prediction and evidence file even when models fail.

#### Reliability and budget

- Read credentials and endpoint from the environment through `llm.py`.
- Let the existing wrapper retry briefly, fall back within a tier, and stop
  retrying a model after repeated capacity failures.
- Degrade to the deterministic candidate when all model calls fail.
- Keep prompts compact and outputs short.
- Avoid repeated scans of multi-gigabyte files.
- Target substantially less than one minute per case on average.
- Keep cost comfortably below the $1.25 judged-run average per case.

#### Evaluation scope

Required first comparison: single strong-model configuration versus the routed
hybrid agent on the same development cases. Add the official metric-only
heuristic as a third configuration if time permits. Defer Flash-only,
trace-only, and repeat-variance studies until the core comparison works.

Use a new experiment directory from the start of each run. The following
development commands run from the repository root; use the same case manifest
for both configurations and inspect output completeness in addition to the
official scorer. These are paid runs when the agent invokes models:

`RCA_MODE` is part of the new controller contract to implement; the original
starter does not yet use that setting. These commands target the integrated Agent.

```bash
env -u RCA_MODEL RCA_MODE=routed python track-1/starter/run.py \
  --dataset track-1/data/Market-cloudbed-1 \
  --queries track-1/data/Market-cloudbed-1/query.csv \
  --out track-1/out/comparison-01/routed --agent agents.routed --limit 20
RCA_MODE=routed RCA_MODEL=zai-org/GLM-5.2 python track-1/starter/run.py \
  --dataset track-1/data/Market-cloudbed-1 \
  --queries track-1/data/Market-cloudbed-1/query.csv \
  --out track-1/out/comparison-01/single --agent agents.routed --limit 20
# Score each predictions.csv with starter/score.py and price each usage.jsonl
# with starter/cost.py. Use a NEW comparison directory for each repetition.
```

`RCA_MODEL` currently pins model identity; it does not force a model call.
Preserve that override when implementing routing and report deterministic
bypasses in both runs. An always-strong comparison would require an explicit
routing override and must be labeled separately.

Record accuracy, fully solved cases, dollars per case, runtime, model calls,
and escalation rate. Break down scores by task and difficulty using the starter
scorer. Hold back development cases for evaluation or disclose tuning overlap.
Spot-check evidence against raw telemetry and categorize failures as timestamp,
component, reason, node/container, network, multiple-failure, or formatting.

## Shared Interface and Team Ownership

Use a small shared structure across the implementation:

```text
analyze_case(instruction, dataset_dir)
    -> parsed incident and metric candidates
    -> trace evidence
    -> targeted log evidence
    -> ranked candidates
    -> routed decision
    -> validated prediction + deterministic evidence
```

Each candidate carries a stable ID, exact component, possible legal reasons,
onset, persistence, score features, supporting and contradicting evidence,
and propagation links. Evidence records include the source file, component or
edge, time window, and measured values. Model responses select supplied
candidate/reason IDs; deterministic code retains measured timestamps and facts.

Five-person ownership is defined in [TEAM.md](TEAM.md):

- **M1 — Data and integration:** shared schemas, query access, time/identity,
  runtime state, dependencies, entry point and Docker.
- **M2 — Metrics and onset:** resource features, change intervals, persistence,
  replica and node comparisons.
- **M3 — Traces and logs:** independent trace candidates, dependency edges,
  trace/network observations and targeted logs.
- **M4 — Controller and routing:** candidate fusion/ranking, bounded follow-up,
  confidence gating, GLM calls, fallback and actual-call accounting.
- **M5 — Evidence and evaluation:** deterministic rendering, validation,
  complete-denominator comparisons and the submission report.

Only M4 orchestrates investigations. M2/M3 return observations; they do not start
their own agent loops or choose final answers. Shared types are owned by M1 and
specified once in `docs/INTERFACES.md`.

Agree on the shared schema first, then integrate a working path through all six
steps before adding detector sophistication.

## Deferred Work and Presentation

Do not build full-day preprocessing, Parquet caches, broad telemetry indexes,
log-template clustering, heavy learned anomaly detectors, or complete separate
application/network detector systems for the first version. Prioritize a
container/node metric detector, trace/network analysis, and simple log search.

Skip Harzoo for this hackathon. Plain Python functions can later become MCP
tools; MCP is an extension point, not a runtime requirement. Phoenix may be
added for development tracing after the pipeline works, outside its critical
path.

The demo should show an SRE-style investigation: service health checks narrow
the suspects, traces explain propagation, targeted logs support fault labels,
and measured evidence explains the decision. Show easy cases solved without a
model, ambiguous cases sent to Flash, and difficult cases escalated to a strong
GLM. Support the accuracy, cost, latency, and explainability story with the
measured comparison rather than assuming routing improves all four.

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
