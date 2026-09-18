# Authorized two-case provider integration

Frozen runtime revision: `7bc31e71d0b02aa44bef5c7da004dee68fa4f62b`. Original public development IDs: 6 and 25, in that order. One repetition per configuration; same code, tools, data identity, order, prices and budget policy. Native Windows; no container resource enforcement and no OS-cache control.

| Configuration | Returned | Mean partial | Fully solved | Requests | Estimated cost | External elapsed |
|---|---:|---:|---:|---:|---:|---:|
| Single GLM-5.2 | 2/2 | 0.335 | 0/2 | 2 | $0.4137852 | 201.875 s |
| Routed | 2/2 | 0.500 | 1/2 | 2 | $0.019789005 | 306.718 s |

Two preliminary probes add $0.000049875, bringing the whole authorized experiment to **6 requests and $0.43362408 estimated spending**. All six calls returned token usage, including the empty responses. Pricing uses observed provider tokens and the frozen official price table; no account billing receipt was obtained.

Both routed case calls returned empty content and used deterministic fallback. The higher routed score is not evidence that its model reasoning improved accuracy. Three of four case calls returned empty content; only the single-model second-case selection was accepted. There were no fallback-model or strong-stage calls. Requests exceeded their configured timeout, and all four cases exceeded the 45-second internal soft target. The subsequent transport and prompt corrections have separate offline verification; this run does not measure the corrected code.

`summary.json` includes probes in total spending. Per-configuration `audit.json` keeps the entire planned denominator and excludes raw development-answer strings. `routes.jsonl` includes every attempted request and explicit bypass. `predictions.csv` contains generated answers. Source hashes and numerical measurements are preserved; absolute workspace prefixes are replaced with `<repo>` for portability. Raw telemetry, full evidence ledgers and credentials are not included.

Full private outputs remain under `out/glm-smoke-20260917/`. See the root `REPORT.md` for interpretation and remaining acceptance limits.
