# Track 2 execution plan. 3 people, T-2h25m

**Deadline: 3:00 PM PDT.** Demos 3:05 PM. Written at 12:33 PM.
**Golden rule from the organizers' own slides: "Working > Perfect. Ship real functionality."**

---

## The one-paragraph pitch we are building toward

> 83% of this cluster's GPU-hours didn't turn into completed work. Most teams will
> tell you that number. We'll tell you which **$X of it is actually recoverable**,
> how sure we are, and, the part nobody else will show you, we caught MantisGrid's
> own recommendation engine telling us to drain a machine that isn't broken. Here's
> the receipt.

Every task below exists to make that paragraph true and demoable. If a task doesn't
serve it, cut it.

---



## Hard scope freeze

**SHIP (non-negotiable):**

1. `docker compose up` → dashboard on `:3000`, zero manual steps
2. Three tiles: where the money goes / where to cut / what it costs if we're wrong
3. Drill-down: dollar figure → finding → raw job rows
4. An MCP-backed agent panel that reasons live
5. `claims.json` (validated) + `REPORT.md` + README with AI disclosure
6. The Layer-B falsification demo (our differentiator)

**EXPLICITLY CUT. Do not start these:**

- Queue-tail engineer-hours analysis (interesting, not scoreable in time)
- Drain-cost threshold modeling
- All 113 node-triage entries (we do 6–8, deeply)
- Card-imbalance deep dive (fill the claim only if Person A has spare time)
- Auth, multi-user, responsive mobile polish, dark mode
- Any rewrite of the API or MCP layer. **they are done, we consume them**

---



## Timeline (wall clock)


**Rebalanced at 12:38.** C's original scope was blocked on A's numbers for the first 40
minutes while B was overloaded. C now owns the **agent panel** (lifted from B) and the
**drill-down UI** (split with B), so all three are loaded from minute one.

| Time        | A, Data Truth                                     | B, Dashboard shell + tiles               | C, Agent + drill-down + claims    |
| ----------- | -------------------------------------------------- | ----------------------------------------- | ---------------------------------- |
| 12:40–12:55 | **Everyone: data pipeline running** (see T0 below) | scaffold app, get `:3000` serving         | repo hygiene, then **agent loop scaffold** |
| 12:55–13:30 | headline numbers + dedup                           | tiles 1&2 wired to live API               | agent panel reasoning live         |
| 13:30–14:10 | node triage + Layer-B falsification                | tile 3 + drill-down wiring                | drill-down panel + claims.json     |
| 14:10–14:30 | hand final numbers to C                            | **integration freeze**, wire A's numbers | validate, clean-clone test         |
| 14:30–14:45 | help B/C                                           | bug-fix only                              | demo script + rehearse once        |
| 14:45–15:00 | **SUBMIT** (buffer for form + push)                |                                           |                                    |


**14:10 is the integration freeze.** After it: no new features, only wiring and fixes.
**14:45 submit.** Do not aim for 15:00, the form takes time and the clock is theirs, not yours.

---



## T0. Do this in the next 15 minutes, all three of you

One person runs this; the other two watch and start scaffolding. It is pure I/O wait.

```bash
cd "track-2"
curl -O https://mantisgrid-hackathon.s3.us-east-1.amazonaws.com/track-2-raw.zip
unzip track-2-raw.zip -d data/raw
make prep        # ~1 min, writes data/prepped/
make generate    # seconds, writes data/synthetic/
make check-data  # MUST print "Your data matches."
```

**If** `check-data` **says MISMATCH, stop and fix it before any analysis.** Mismatched
data means your findings differ from what the judges see, and every number you
produce is unverifiable against their ground truth.

Then, in a second terminal:

```bash
docker compose up        # API on :8000
curl localhost:8000/health
```

Meanwhile, git hygiene (Person C, 2 min):

```bash
git checkout -b submission          # do NOT commit on the org's main
echo "data/" >> .gitignore          # licence forbids redistributing derived data
```

---



# PERSON A. Data Truth & Falsification

**Plain English:** you are the reason our numbers survive a judge asking "where did
that come from?" You also build our single biggest differentiator.
**Technical:** compute defensible aggregates from `data/prepped/*.parquet`, then
adversarially audit the API's own Layer B.

Work in a notebook (`docker compose up` gives you Jupyter on `:8888`) or a plain
script. Speed over structure. **Write every number you produce into a shared
scratch file the moment you have it**. `NUMBERS.md`, plain text, so C can lift
them into `claims.json` without asking you.

### A1 (12:50–13:10). The headline waterfall

Read [FIELD_REFERENCE.md](FIELD_REFERENCE.md) §5 before you sum anything.

```python
import pandas as pd
jobs = pd.read_parquet("data/prepped/jobs.parquet")
gpus = pd.read_parquet("data/prepped/gpus.parquet")

ALLOCATED = jobs.gpu_hours.sum()          # must be ~594,004, your ceiling
by_state  = jobs.groupby("state_name").gpu_hours.sum().sort_values(ascending=False)
```

Then the recoverable number. **Method (defensible, fast, and states its own limits):**

1. Load all findings: `mg.findings_df()` (~24 requests, keep the DataFrame).
2. Keep **job-scope only** (`metadata_impact_scope == "job"`), never mix scopes.
3. **Deduplicate to unique** `job_id`, then join to `jobs.gpu_hours` and sum the
  *jobs'* real hours, not the findings' `impact_gpu_hours`. This is the 311,373
   figure's method and it is the only one that can't exceed the cluster.
4. Split that total by `impact_kind` and report kinds separately.
5. Apply a **recoverability haircut** and *say it out loud*: not all wasted hours are
  recoverable. A defensible v1: count `unused_capacity` + `consumed` as addressable,
   exclude `lost` (work destroyed. Rerunning it still costs the hours), then take a
   fraction you can argue for. **Whatever you choose, the** `basis` **string must name it.**

**Sanity gate. Do not publish a number that fails this:**

```python
assert recoverable <= ALLOCATED, "impossible: exceeds cluster capacity"
print(f"{recoverable:,.0f} GPU-h = {recoverable/ALLOCATED:.1%} of allocated")
```

**The CANCELLED decision.** 203,930 GPU-h, ~34% of the cluster, swings the headline
~2×. Recommendation: `cancelled_is_waste: false`, because a user killing a bad run
early is correct behaviour and counting it as waste would tell the CFO to discourage
it. **But** carve out the subset that *is* waste: `rules::slow-cancel-of-idle-job`
(949 findings). Jobs that sat idle >4h before anyone noticed. That's a detection-latency
problem, not a cancellation problem, and separating the two is exactly the nuance judges
are looking for. Give C both numbers.

### A2 (13:10–13:35). Node triage, 6–8 entries done properly

The schema is explicit: `reasoning` **is the mark.** Quality over coverage. Ten
`cannot_determine` with real joins shown beats 113 guesses.

```python
elev = mg.findings_df(detector_id="rules::node-elevated-failure-rate")
```

For each node you pick, compute and record the actual numbers:


| Verdict            | The test you must show                                                                                                                           |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `user_code`        | one `id_user` owns ≥90% of that node's failures in the window **AND** others on the same machine in the same hours succeeded, report both rates |
| `hardware`         | several *unrelated* users hit the same `exit_code // 256` on this node and essentially never elsewhere, report the elsewhere-rate               |
| `workload_mix`     | the node's failures are concentrated in one `partition`/`job_type` that fails everywhere at a similar rate                                       |
| `cannot_determine` | say which test you ran and what number stopped you                                                                                               |


**Trap:** use `gpus.parquet` grouped by `Node` for per-node hours, never `primary_node`
(it's the first node only; 1,472 multi-node jobs carry 27.8% of hours). And check
`hit_node_failure`, never `state_name`, for hardware signal.

Cross-check each candidate against `mg.causal(finding_id)` where a chain exists.

### A3 (13:35–14:10). 🔥 THE DIFFERENTIATOR: falsify `rec_drain_nodes`

This is the highest-value 35 minutes anyone on the team will spend. Read
[api/main.py:571-580](api/main.py#L571-L580), the recommendation ranks nodes by **raw
finding count** and says "drain and submit for hardware inspection", `confidence: 0.58`,
`effort: low`. The API's own docstring at [api/main.py:511-516](api/main.py#L511-L516)
admits it "does not read `rootCauses`, so correlated findings sharing one cause are
counted as independent problems."

`docs/rules.md` on `array-task-failure` closes the loop: *"The machines are innocent…
if a node were at fault the failures would concentrate on it. They share an exit code
instead."*

**So: a node that merely hosted tasks from one mass-failing array accumulates a high
finding count and gets recommended for draining. Prove it.**

```python
recs  = mg.recommendations()
drain = next(r for r in recs if r["id"] == "rec_drain_nodes")

for fid in drain["finding_ids"]:
    ch = mg.causal(fid)
    # record: does the chain resolve to the NODE, or to an array / a person?
```

For each of the 5 recommended nodes, produce one row:


| node | findings | what causal() says the cause is | drain justified? | hours at risk if drained |
| ---- | -------- | ------------------------------- | ---------------- | ------------------------ |


Then compute the **cost of the bad advice**, this is what makes it a CFO story rather
than a code nitpick: take the node's real delivered GPU-hours from `gpus.parquet` and
show what draining it destroys in capacity against the savings the API claimed.

**Deliver to C:** the table, and one sentence of the form *"Recommendation*
`rec_drain_nodes` *claims $N savings at confidence 0.58; on our audit, K of 5 nodes
resolve to a non-hardware cause, so acting on it would drain M GPU-hours of healthy
capacity to fix a problem that isn't on those machines."*

**If the audit comes back clean**, all 5 genuinely resolve to hardware. **say so
honestly and pivot the framing to "we verified it, here's the method."** A negative
result you can defend is still a differentiator; a fabricated one is disqualifying.
The organizers' Track 1 brief literally says *"a reasoned negative result beats a
vague claim."*

### A4. If and only if there's time

`hardware_attributable_failures`: conservative count = jobs with `hit_node_failure`
(31, not the 10 that end in NODE_FAIL). Rationale must state you counted attempts not
final states, and whether you included the silent fault. **Never attribute all failures
to hardware, that is the one explicitly wrong answer.**

---



# PERSON B. Dashboard shell + tiles + integration owner

**Plain English:** you build the thing judges look at, and you own the contract that
lets three people work on one app without colliding.
**Technical:** a one-command web app on `:3000`, tiles 1–3 live from the API, and the
API contract + mount points that C builds the agent and drill-down panels into.

**You own the interfaces.** Your first job after `:3000` serves is publishing the
contract (B0.5) so C can build in parallel instead of waiting on you. You are also the
integration owner at 14:10. Everything merges through you.

> **Scope moved to C in the 12:38 rebalance:** the agent panel (was B3) and the
> drill-down panel UI (was B2) now belong to C. You still own tile 1, tile 2, tile 3,
> the shell, the compose wiring, and the drill-down *data contract*.

### B0 (12:35–12:55). Serve something on :3000 immediately

Pick the stack you type fastest in. Suggested: **FastAPI serving one static HTML page +
vanilla JS** `fetch`, because it adds one service to compose and zero build step.
A Vite/React app is fine *if* you already have the muscle memory, but a broken
`npm run build` at 14:30 loses the hackathon, and a single HTML file cannot.

Add to `docker-compose.yml` (keep the existing `api` service. Required by the brief):

```yaml
  dashboard:
    build: {context: ., dockerfile: dashboard/Dockerfile}
    ports: ["3000:3000"]
    environment: [MGAI_URL=http://api:8000]
    depends_on:
      api: {condition: service_healthy}
```

**Milestone: by 12:55** `docker compose up` **must serve *something* at :3000.** Even
"Hello" with a live `/health` readout. Everything after is filling it in.

### B0.5 (12:55–13:00). Publish the contract, then never break it

Five minutes that unblock C for the next hour. Post this in chat/Discord the moment
`:3000` serves, and treat it as frozen:

- **DOM mount points** C owns and you never touch:
  `<div id="agent-panel">` and `<div id="drilldown-panel">`
- **Backend routes** C owns: anything under `/api/agent/*`. You own `/api/tiles/*`.
- **The drill-down call C consumes:** you expose
  `GET /api/findings?detector_id=&job_id=&limit=` returning the finding list plus
  joined job rows. C renders it; you supply it.
- **Files:** you edit `dashboard/index.html` + `dashboard/tiles.js`;
  C edits `dashboard/agent.js` + `dashboard/drilldown.js`. **Separate files = no merge
  conflicts.** Do not both edit `index.html` after 13:00. If the markup needs to
  change, C asks you.

If the contract needs to change, say so out loud immediately. A silent contract change
at 14:00 costs more than the feature.

### B1 (12:55–13:30). Tiles 1 & 2, live from the API

Never hardcode a number. Every figure comes from an endpoint at request time, because
judges regenerate `data/` themselves and stale constants will be visibly wrong.

**Tile 1. Where the money is going.** `GET /v1/efficiency/summary` (the waterfall:
allocated → computed → computed_completed) + `GET /v1/waste/breakdown` (by outcome).
Show **dollars first, GPU-hours second, % of capacity third**, the CFO reads left to
right and she is not an engineer. Put the `provenance.caveat` in a tooltip or footnote,
not buried: quoting a caveated number *with* its caveat is what separates a trustworthy
dashboard from a confident one.

**Tile 2. Where to cut.** Ranked, specific, owned. Not "improve utilization." Each row:
the action, the estimated recovery in $, effort, confidence, and a click target. Source
from `/v1/recommendations` **but render our audited verdict beside each one** (A3's
output), this is where your work and A's fuse.

### B2 (13:30–13:50). The drill-down *endpoint* (C renders it)

*"Can you click from a business number to the raw data?"*. Explicitly judged. The path
must be unbroken:

```
$ figure  →  recommendation  →  finding_ids  →  finding detail (metadata.job_id)
          →  the actual job rows behind it
```

**Your half:** expose `GET /api/findings?detector_id=&job_id=&limit=` that takes a
finding id or job id and returns the finding (`shortDescription`, `impact_gpu_hours`,
`impact_kind`, `job_id`) **joined to the real job rows** from `jobs.parquet`. That join
is the whole point. It's what turns a dollar figure into raw evidence.

**C's half:** the panel that renders it. You supply data, C supplies UI.

### B3 (13:50–14:10). Tile 3: what it costs if we're wrong

The brief calls this *"the tile most teams skip, and the one we care about most."*
C owns the content (the interval, the scenario framing); you own the rendering.

Show the headline recoverable number **with its confidence band drawn**, not just a
point estimate with a ± in text. One line beneath it on why it's that wide. If A's
CANCELLED decision creates a scenario range, show both scenarios as distinct bars, a
CFO understands "under assumption X it's this, under Y it's that" far better than a
single number with an error bar.

### B4 (14:10–14:30). Integration owner

Everything merges through you. Pull C's `agent.js` and `drilldown.js`, wire A's final
numbers, confirm `docker compose up` still comes up clean from scratch. **No new
features after 14:10**. Wiring and fixes only.

---



# PERSON C. Agent panel + drill-down UI + claims/report/demo

**Plain English:** you build the AI that reasons on stage, the single piece that proves
AI is *central* to this project. Plus the click-through that makes our numbers
trustworthy. Then you own the 4 minutes that decide everything.
**Technical:** an agent loop over the MCP/API tools with a visible reasoning trace, the
drill-down panel, `claims.json` to schema, `REPORT.md`, and the demo.

**You own judging criterion #4 outright** ("Is AI central to the solution rather than
simply added on?"), one of five criteria, and the one most teams fake. You also own the
technicalities that can disqualify us. This is the most front-loaded role on the team:
your hardest work happens first, while A is still computing.

**Files you own (never edit B's):** `dashboard/agent.js`, `dashboard/drilldown.js`,
plus backend routes under `/api/agent/*`. Mount into `<div id="agent-panel">` and
`<div id="drilldown-panel">` per B's contract.

### C1 (12:40–12:50). Repo hygiene (10 min, then move on)

```bash
git checkout -b submission
echo "data/" >> .gitignore
```

Stub the README with the AI-disclosure section. Per `docs/submission.md` it's
**required**: list the AI models, coding assistants and agent frameworks used, and say
briefly what was AI-generated vs. team-written. *Using AI heavily is expected here; not
disclosing it is the problem.* Two bullets now, finish it at 14:30.

### C2 (12:50–13:30). 🤖 THE AGENT PANEL (your differentiator, start immediately)

Do **not** wait for A's numbers, the agent reads the API directly, so you can build
this the moment the data pipeline finishes.

Judging criterion #4 is verbatim *"Is AI central to the solution rather than simply
added on?"*, and the slides say twice to build chat/agentic experiences on the MCP
tools. A dashboard with no agent forfeits a fifth of the rubric.

**The whole trick: make it visibly reason, not just answer.** Step-render each tool call
so the judge *watches* `list_findings → causal → verdict` happen. The reasoning trace
IS the demo, a chat box that emits one paragraph looks identical to a hardcoded string,
and judges know it.

Two routes. Pick by what you'll get working fastest:
- **(a) Real MCP:** `make mcp` (needs `uv`), connect an LLM client with tool access.
  Highest credibility; more moving parts.
- **(b) Agent loop over the same endpoints:** an LLM with function-calling pointed at
  `mgai_client.py`'s methods (`findings`, `causal`, `recommendations`, `underperforming`).
  Equivalent reasoning, far fewer failure modes. **Recommended at this clock**, the MCP
  server is in the repo being consumed either way, and `mcp_layer/server.py` is your
  reference for what each tool means and when to distrust it.

**Give the agent the epistemics in its system prompt.** This is the highest-use
paragraph you'll write today. Lift it from `mcp_layer/server.py`'s own instructions:

> Layer A (`findings`, `causal`, `neighbor`, `rules`) is authoritative. Layer B
> (`recommendations`, `underperforming`, …) is a *proposed* business layer. Every
> response carries `kind`: `fact`, `judgment`, or `simulated`. **Never act on a
> `judgment` without validating it against `causal`.** `underperforming` and
> `recommendations` do not read `rootCauses`, so they count correlated findings sharing
> one cause as independent problems.

An agent that *spontaneously distrusts the recommendation endpoint and checks it* is the
demo moment. That's not a canned answer. That's the tool descriptions doing real work.

**Seed 3 questions that always work** (type these in the demo, don't freestyle):
1. *"Which nodes are recommended for draining, and does the causal evidence support it?"*
2. *"How much of our recoverable estimate comes from cancelled jobs, and should it count?"*
3. *"Show me the evidence behind the largest single finding."*

**Non-negotiable:** if the LLM call fails live, degrade to the pre-computed answer, not
a stack trace. Wrap every call. **Test the failure path once**. Pull your API key and
confirm the panel still renders something sane.

### C3 (13:30–14:00). Drill-down panel + claims.json

**Drill-down (13:30–13:45).** B gives you `GET /api/findings?..` returning findings
joined to job rows. You render the panel: click a tile row → list the contributing
findings (`shortDescription`, `impact_gpu_hours`, `impact_kind`, `job_id`) → expand to
the raw job rows. **A plain table is enough**, this is about the path existing, not
beauty. The CFO's SRE will click it; if it dead-ends she stops trusting the number, and
the brief says exactly that.

**claims.json (13:45–14:00).**

Validate against [starter/claims.schema.json](starter/claims.schema.json). Key facts
from the schema that shape your strategy:

- **Only** `recoverable_gpu_hours` **is required.** Everything else is optional.
- **Omitted costs nothing; confidently wrong costs a lot.** If A didn't investigate it,
leave it out. Resist the urge to look thorough.
- `interval_kind` **is a free win most teams will miss.** It takes `"uncertainty"`
(statistical spread) or `"scenario"` (the number under a different defensible
assumption). Our CANCELLED decision is *exactly* a scenario interval. Using
`"scenario"` and saying so proves we understand what our own interval means.
- `node_triage[].reasoning` **is "THE MARK"** in the schema's own words. Paste A's
actual computed numbers and column names. Not "analyzed failures". Rather
"grouped this node's FAILED jobs by `id_user`; one user owned 84% (37/44) while
others on the same machine in the same 48h window failed 11%."
- `cannot_determine` is correct on ~a quarter of them, per the schema. Use it.

Intervals: **wide and honest beats narrow and wrong.** If A's point estimate rests on a
haircut assumption, the interval should span the plausible range of that assumption,
and `basis` should say so.

### C4 (14:00–14:20). `REPORT.md`

Write this fast and structurally. A's falsification table pastes straight into §4.
Structure, in this order (front-load the differentiator):

1. **Headline**. Recoverable $, with interval and confidence, in one sentence.
2. **Method**. How we deduplicated, why our total can't exceed 594,004, what we
  excluded and why. Name the traps we avoided (scope mixing, `primary_node`,
   `.first()`, row-vs-hour weighting).
3. **Where to cut**. Ranked, owned, with effort.
4. **⭐ Where we disagree with the API**. A3's falsification, with the table.
5. **What it costs if we're wrong**, the calibration section.
6. **What we did not do**. Scope honesty. Judges read this as maturity, not weakness.
7. **AI disclosure** (or in README, but say it somewhere).



### C5 (14:20–14:35). Validate + clean-clone test

```bash
make validate CLAIMS=claims.json
make validate CLAIMS=claims.json URL=http://localhost:3000
```

Then **do exactly what the judges do**, this catches the failure that kills submissions:

```bash
cd /tmp && git clone <our-repo> judgetest && cd judgetest
# regenerate data/ the way data/README.md says (steps 1-4)
docker compose up
# open localhost:3000
```

If it doesn't come up, it cannot be judged. **Do not skip this even if you're behind** -
a broken boot is the single most common way a finished project scores zero.

### C6 (14:35–14:45). Demo script, rehearsed once out loud

4 minutes, one or two presenters. Beat sheet:


| Time      | Beat                                                                                                                                                                                          |
| --------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0:00–0:30 | The CFO problem. One sentence. Land the headline $ with its interval.                                                                                                                         |
| 0:30–1:30 | Tiles 1 & 2 on screen. Where the money is, where to cut, who owns it.                                                                                                                         |
| 1:30–2:15 | **Drill down live.** Dollar → finding → raw rows. "Her SRE clicks this and it holds."                                                                                                         |
| 2:15–3:15 | **⭐ The falsification.** Ask the agent the drain question. Let the judges watch it reason. Land: "the API said drain; the evidence says otherwise; here's what that mistake would have cost." |
| 3:15–3:45 | Calibration: our interval, why it's that wide, what we'd need to narrow it.                                                                                                                   |
| 3:45–4:00 | What we cut and why. Close.                                                                                                                                                                   |


Rehearse **once**, out loud, with a timer. Every hackathon team overruns; the ones that
practice once don't.

---



## Things to remember (the list that loses hackathons)

**Numbers**

- [ ] No total exceeds **594,004 GPU-hours**. Ever. Check before it renders.
- [ ] Never sum `impact_gpu_hours` raw (naive = 157% of cluster, impossible)
- [ ] Never add across `impact_scope` (job vs user vs cluster = different denominators)
- [ ] Never add across `impact_kind` (destroyed ≠ never-used)
- [ ] 5 rules carry no GPU-hours on purpose. Keep them out of $ totals
- [ ] `gpus.parquet` + `.sum()` for per-node; never `primary_node`, never `.first()`
- [ ] `hit_node_failure` not `state_name` (loses 2/3 of hardware signal)
- [ ] Hour-weighted, not row-weighted (a third of rows = 0.012% of compute)
- [ ] `mem_req_mb` not `mem_req`; `energy_wh` not `energyconsumed_joules`
- [ ] Don't conclude "fix the data pipeline". `gpu-pcie-saturated` has never fired;
  ```
  the bus peaks at 27% of capacity and that's the evidence against it
  ```
- [ ] Flag the filesystem incident as synthetic if we mention it (`metadata.synthetic`)

**Dashboard**

- [ ] `docker compose up`, **one command, unattended**, `:3000`
- [ ] Keep the `api` service in compose
- [ ] **Don't commit** `data/` (licence). Judges generate it themselves
- [ ] Only ONE `docker-compose.yml` in the repo, at the root
- [ ] No hardcoded numbers. Judges' regenerated data will expose them
- [ ] **No per-user blame ranking.** Users are hashed and stay that way. A dashboard
  ```
  that ranks employees by waste is called "hostile" in the brief and scores lower.
  Frame everything as *capacity*, not culprits.
  ```
- [ ] Say on the dashboard that this is a 4-month **sample**, not the whole cluster
  ```
  (MIT's licence terms ask for it and a careful judge will check)
  ```

**Submission**

- [ ] Public repo, secrets removed, **work merged & pushed to the default branch** -
  ```
  they judge whatever the link shows when they clone it
  ```
- [ ] `claims.json`, `REPORT.md`, `docker-compose.yml` at repo ROOT
- [ ] README lists AI tools + what was AI-generated
- [ ] Form: team + each member's student/career status, title, description, track,
  ```
  repo link, ~4-min presentation
  ```
- [ ] **Submit by 14:45.** After the deadline you may fix bugs; you may **not** add features.

---



## Differentiation, per person

Everyone will ship three tiles and a dedup'd total. Here's each person's edge:

**A, the adversarial audit.** Nearly every team will consume `/v1/recommendations`
as truth and put it on a slide. A independently falsifies it with `causal()` and prices
the error. The brief *invites* this ("Argue with us… showing, with evidence, that one of
its endpoints has the wrong shape is exactly what we want") and almost nobody will have
the composure to do it under time pressure. **Innovation + Problem & Impact.**

**B, the unbroken evidence path + calibration made visible.** Two things most teams
botch. First, the drill-down join: everyone shows a dollar figure, few can click it
down to raw job rows, and the brief judges exactly that. Second, everyone fills
`low`/`high` in `claims.json` and then never surfaces it. B *draws* the confidence band
on the headline number, with both CANCELLED scenarios as separate bars. The brief calls
the cost-of-being-wrong tile *"the one most teams skip, and the one we care about most."*
**Technical Execution + Problem & Impact.**

**C, the agent that visibly reasons, and distrusts on its own.** Most teams' "AI" will
be a chat box that paraphrases a number. Indistinguishable from a hardcoded string.
C's steps through its tool calls (`list_findings → causal → verdict`) so judges watch
the reasoning happen, and because its system prompt carries the `fact`/`judgment`
epistemics, it *spontaneously validates Layer B against `causal` rather than trusting
it.* That is criterion #4 verbatim. AI central, not added on. **Use of AI/Agents +
Demo & Storytelling.**

**Team-level theme:** everyone shows judges *what they found*. We show *how sure we are
and how we checked ourselves*. Including checking the organizers' own API. That's the
axis nobody spends time on, because it doesn't feel like building.

---



## If you fall behind. Cut in this order

1. Card imbalance claim (drop entirely). *A*
2. `hardware_attributable_failures` (optional field, omit it). *A*
3. Node triage: 8 entries → 4. *A*
4. Tile 3's two-scenario bars → one confidence band on the headline. *B*
5. Agent panel → pre-computed reasoning trace, rendered step-by-step. *C*
   *(still shows the reasoning; costs the "live" wow, keeps criterion #4)*

**Never cut:** the one-command boot, the drill-down path, `claims.json` validating,
or the falsification story. Those four are the project.

## Load-balance notes (why this split, if you're rebalancing again)

- **C is front-loaded, A is back-loaded.** C's hardest work (the agent) needs only the
  API, so it starts at 12:50. A's falsification can't finish until findings are loaded
  and audited, so it lands ~14:10. If C finishes the agent early, they help A audit
  the 5 drain nodes, that work parallelizes cleanly across people.
- **B is the integration bottleneck by design.** B owns the shell and the contract so
  two people can build panels into it without merge conflicts. If B falls behind on
  tiles, A takes tile 1 (they already have the numbers in a notebook).
- **Don't let anyone idle waiting on numbers.** Agree on placeholder constants at 13:00
  and wire the UI against them; swap in A's real values at 14:10. A dashboard wired to
  placeholders is 10 minutes from done; one not yet built is 40.