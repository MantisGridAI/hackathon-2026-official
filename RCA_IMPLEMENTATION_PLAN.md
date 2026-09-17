# RCA Agent Implementation Plan

## Goal

Build a fast, cost-efficient root-cause analysis agent that uses deterministic
telemetry analysis first, calls a cheap Featherless GLM for uncertain cases, and
calls a stronger GLM only when the cheap result is genuinely ambiguous.

The agent must identify the occurrence time, exact component, and one of the 15
legal failure reasons. It must also write evidence for every case and remain
within the judging limits.

## 1. Protect the Existing Interface

- Keep `starter/run.py` and its output contract intact.
- Implement the improved agent as a new module under `starter/agents/`.
- Use `format_prediction()` so keys remain in the required order.
- Always emit exactly the number of failures stated in the instruction.
- Always emit a best guess, even when confidence is low.
- Keep all component and reason strings exact.

Checkpoint:

```bash
make validate AGENT=agents.<new_agent>
```

## 2. Normalize and Index the Data

Create a preprocessing layer that reads each telemetry day once and exposes
small, reusable summaries.

- Parse all CSV data with a proper CSV reader.
- Treat metric and log timestamps as seconds.
- Treat trace timestamps as milliseconds.
- Convert all displayed answer times to UTC+8.
- Preserve raw timestamps and identifiers for evidence.
- Parse container IDs such as `node-5.shippingservice-1` into node, container,
  service, and replica fields.
- Parse mesh IDs into both communication endpoints.
- Normalize trace status values into success, error, and unknown while keeping
  the original value.
- Do not fill missing telemetry with zero automatically.
- Handle constant metric series instead of discarding them.
- Cache processed data by day, preferably in memory during a run or in Parquet
  when preprocessing time is acceptable in the judged container.

Build these indexes:

- Container-to-node and container-to-service maps
- Per-minute service health summaries
- Per-minute container and node metric summaries
- Per-minute mesh edge summaries
- Log error counts and message templates by component
- Trace summaries by trace, component, and parent-child edge

Checkpoint: verify that one known timestamp from metrics, logs, and traces lands
in the same UTC+8 incident window.

## 3. Parse Each Question Deterministically

Extract without an LLM:

- Date
- Start and end time
- Failure count
- Task type
- Fields requested by that task

Reject or safely fall back when parsing fails. Never spend a model call on basic
question parsing unless the deterministic parser has failed.

## 4. Detect Incident Onset

Start with the small `metric_service.csv` file.

- Detect the first sustained success-rate drop.
- Detect the first sustained response-time increase.
- Detect meaningful request-rate or count changes.
- Compare the incident window with a baseline before and after it.
- Prefer change-point onset over the largest peak.

Use these results to shortlist affected services and likely occurrence times.

## 5. Reconstruct Causality from Traces

For traces in the incident window:

- Group spans by `trace_id`.
- Join `parent_span` to `span_id`.
- Reconstruct request order.
- Find the earliest component with abnormal latency or errors.
- Measure caller duration versus child duration.
- Detect missing expected child spans.
- Penalize components whose anomaly begins only after an upstream failure.

Use traces to determine propagation order, not merely anomaly size.

## 6. Run Targeted Fault Detectors

Only inspect expensive telemetry for shortlisted services, components, nodes,
and communication edges.

### Container resource detector

- Compare CPU, memory, read I/O, and write I/O with baseline behavior.
- Compare a suspect container with sibling replicas.
- Look for termination or abrupt telemetry disappearance.

### Node detector

- Check whether multiple unrelated containers on one node degrade together.
- Compare node anomaly onset with container anomaly onset.
- Prefer a node cause when the node changes first and has multiple colocated
  victims.
- Prefer a container cause when one container changes first and explains the
  later node aggregate.

### Application detector

- Count service-log errors and detect new error templates.
- Use runtime metrics where available.
- Look for long work inside destination spans.

### Network detector

- Inspect mesh counters for the affected source-destination edge.
- Inspect proxy logs for retries, resets, refused connections, and timeouts.
- Measure unexplained parent-child trace gaps.
- Require fault-specific evidence before distinguishing latency, packet loss,
  retransmission, and corruption.

## 7. Rank Evidence-Based Candidates

Create a structured candidate object containing:

- Exact component name
- Suggested legal reason
- First change time
- Direct supporting signals
- Contradicting signals
- Healthy-replica comparison
- Node health comparison
- Downstream effects explained
- Confidence score

Candidate scoring should reward:

- Early onset
- Direct fault-specific evidence
- Agreement across telemetry types
- Ability to explain downstream failures
- Contrast with healthy replicas or nodes

Candidate scoring should penalize:

- Late downstream symptoms
- Evidence that applies only at service level
- Conflicting telemetry
- Missing support for the proposed reason

For multiple-failure windows, cluster evidence into independent incident chains
instead of selecting the top N correlated symptoms.

## 8. Add Confidence-Gated Model Routing

### Direct answer

Use no model when one candidate has strong direct evidence, a clear lead over
the runner-up, consistent timing, and no major contradictions.

### Cheap-model decision

Use the Flash tier for normal uncertain cases:

```python
FLASH = [
    "zai-org/GLM-4.7-Flash",
    "zai-org/GLM-5.3-Flash",
]
```

Send only a compact structured summary of the top candidates. Require JSON and
constrain choices to supplied components and legal reasons.

### Strong-model escalation

Use the strong tier only when:

- The cheap model reports low confidence.
- The cheap model disagrees with the deterministic leader.
- The top candidates are close.
- Node-versus-container causality is unresolved.
- Network fault subtype is unresolved.
- Evidence conflicts across telemetry sources.
- Multiple failures overlap.
- The cheap response is invalid.

```python
STRONG = [
    "zai-org/GLM-5.2",
    "zai-org/GLM-5.1",
]
```

Give the strong model the candidate summary, contradictions, causal order, and
cheap-model decision. Request one short JSON decision rather than a long report.

## 9. Handle Featherless Failures

- Read `FEATHERLESS_API_KEY` and `FEATHERLESS_BASE_URL` from the environment.
- Use `starter/llm.py` or preserve its usage accounting behavior.
- Detect error objects returned with HTTP 200.
- Retry a model briefly with backoff.
- Fall back within the same model tier.
- Stop offering a model after repeated capacity failures.
- Fall back to the best deterministic answer if all calls fail.
- Record the failure honestly in evidence without leaving the prediction blank.

## 10. Generate Evidence Cheaply

Use a deterministic Markdown template rather than an expensive model.

Include:

- Final answer
- Confidence level
- Measured supporting evidence
- Causal timing
- Alternatives considered
- Reasons alternatives were ruled out
- Any unavailable or missing telemetry
- Models used and fallback events

Never invent measurements. Low-confidence evidence should clearly name the
remaining ambiguity.

## 11. Validate Every Prediction

Before writing output, enforce:

- Exact failure count
- Key order: datetime, component, reason
- Exact component name from the current dataset
- One of the 15 exact legal reasons
- Node components use node reasons
- Container components use container reasons
- UTC+8 timestamp inside the question window
- Distinct and plausible answers for multiple failures
- Evidence file exists

If model output fails validation, repair it from constrained candidates or use
the deterministic fallback.

## 12. Evaluate Incrementally

Do early development on a small set:

```bash
make dev N=20 AGENT=agents.<new_agent>
make score
make cost
```

Then compare at least:

1. Existing heuristic baseline
2. Deterministic improved agent
3. Flash-only agent
4. Strong-only agent
5. Confidence-routed agent

For each configuration, record:

- Strict and partial accuracy
- Accuracy by task and difficulty
- Fully solved cases
- Dollars per case
- Dollars per correct case
- Runtime per case
- Percentage of cases escalated
- Accuracy of escalated and non-escalated cases
- Repeat-run variance

Hold out part of the 70-case development set while tuning thresholds. Categorize
errors into timestamp, component, reason, node/container, network, multiple
failure, and formatting failures.

## 13. Tune for Judging Limits

The judged run has 20 cases and only 20 minutes total.

- Target well below one minute per case on average.
- Set internal time limits below the external limits.
- Avoid repeated scans of large CSV files.
- Keep model prompts compact and outputs short.
- Stop analysis early when evidence is conclusive.
- Ensure a timeout still produces the best answer available.
- Keep average model cost comfortably below $1.25 per case.

## 14. Final Submission Checks

Run:

```bash
make validate AGENT=agents.<new_agent>
make dev AGENT=agents.<new_agent>
make score
make cost
make docker AGENT=agents.<new_agent>
```

Confirm that:

- The improved agent is the default used by `run.py` in the submitted image.
- Docker reads the dataset from `/data` and writes only to `/out`.
- No development answer lookup or deployment-specific component mapping is used.
- Every case writes a prediction and evidence file, even after model failure.
- The final report compares routing with a single-model configuration using
  accuracy, dollars, and runtime.
