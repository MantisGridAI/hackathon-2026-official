# MantisGrid Hackathon 2026

## Team development — Track 1

Start at [TEAM.md](TEAM.md) to claim one of the five modules, find its implementation
specification and AI handoff prompt, and follow the shared interfaces and integration
checks. The implementation source remains `track-1/starter/`. The module documents
specify the implemented five-module Agent. The [module implementation index](docs/modules/README.md)
links each module to its code and tests. See [REPORT.md](REPORT.md) for measured
checks and remaining real-model/container acceptance requirements.

The default entry point now uses `agents.routed`. To run without model calls:

```bash
pip install -r track-1/starter/requirements.txt
RCA_MODE=deterministic python track-1/starter/run.py \
  --dataset track-1/data/Market-cloudbed-1 \
  --queries track-1/data/Market-cloudbed-1/query.csv --out out/my-run
```

In PowerShell, set `$env:RCA_MODE='deterministic'` before the Python command.
For model routing, set `RCA_MODE=routed`, `FEATHERLESS_API_KEY` and optionally
`FEATHERLESS_BASE_URL`. Pin `RCA_MODEL=zai-org/GLM-5.2` for the single-model control.
Use a new output directory per experiment. The sole Dockerfile is at repository root.

Offline tests (synthetic fixtures and local HTTP only):

```bash
cd track-1/starter
python -m unittest discover -s tests -p 'test_*.py'
```

Set `RCA_TEST_DATA` to the official bundle path to include real telemetry smoke tests.
Runtime reads only instruction text and whitelisted telemetry; development labels
are consumed only by the separate offline evaluation tools under `eval/`.

## Official hackathon brief

Two tracks. Pick one.

| Track | The question | You build |
|---|---|---|
| [**Track 1 — Root cause analysis**](track-1/) | *Why did this break?* | an agent that finds the cause of an incident |
| [**Track 2 — Cluster efficiency**](track-2/) | *Why is this wasteful?* | the view that says where to cut GPU spend |

Each track's `README.md` is its brief. Start there.

## Handing it in

**One form, before September 17, 2026, 3:00pm PDT.** Late submissions are not judged.

**https://forms.gle/UbPSwZhKNfkovM8s5**

It asks for your team, your project title and track, a public repository with the
default branch to be judged, and a presentation of around four minutes showing the
project working. Your track's `docs/submission.md` has the rest, including what the
repository has to contain.

## Before you start

- [`PARTICIPANT_AGREEMENT.md`](PARTICIPANT_AGREEMENT.md) — the terms you agree to.
- [`ATTRIBUTION.md`](ATTRIBUTION.md) — where the data comes from, and its licences.

## The data is not in this repository

Each track's README tells you how to get the data.

## AI usage disclosure

OpenAI Codex (GPT-6) and its subagents implemented the five RCA modules, tests,
packaging and evaluation harness under human direction. The official starter,
formatter, heuristic baseline and accuracy evaluator were reused; the official
accuracy evaluator is unchanged. No external agent framework was added. Runtime
uses only the documented Featherless GLM family; per-attempt routing and observed
usage are recorded under each run's output directory. See REPORT.md for actual
experiments and limits; coding-assistant usage is distinct from runtime model calls.
