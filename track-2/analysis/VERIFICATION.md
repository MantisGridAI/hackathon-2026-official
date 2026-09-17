# Independent verification log

Every headline number was re-derived **from scratch by a separate agent** that was
explicitly instructed not to read our analysis scripts, so it could not inherit our
method or our mistakes. Prompt: [`VERIFY_PROMPT.md`](VERIFY_PROMPT.md).

- **Verifier:** OpenAI Codex CLI 0.154.0, model `gpt-5.6-terra`
- **Date:** 2026-09-17
- **Result: 14 confirmed · 2 rounding corrections · 0 unverifiable**

## Claim-by-claim

| # | Claim | Result |
|---|---|---|
| 1 | allocated = 594,004 GPU-h | ✅ CONFIRMED (594,003.840) |
| 2 | GPU-hours by `state_name` | ⚠️ corrected, we omitted `UNDECODED_1024` (1 job, 0.009 GPU-h) |
| 3 | naive finding sum = 931,560 = 156.8% of cluster | ✅ CONFIRMED (931,559.990 / 156.827%) |
| 4 | job-scope only = 537,664 | ✅ CONFIRMED (537,664.110) |
| 5 | 9,322 flagged jobs → 311,420 GPU-h; 1,812 multi-finding | ✅ CONFIRMED (52.428% / 19.438%) |
| 6 | 5-detector union = 2,825 jobs → 150,944 GPU-h | ✅ CONFIRMED (150,943.768) |
| 7 | low 93,873 → midpoint 122,408 → $306,021 @ $2.50 | ✅ CONFIRMED (93,873.048 / 122,408.408 / $306,021.02) |
| 8 | slow-cancel-of-idle = 949 jobs / 71,052 GPU-h | ✅ CONFIRMED (71,052.406) |
| 9 | hit_node_failure 31 · NODE_FAIL 10 · exact 25 | ✅ CONFIRMED |
| 10 | cluster FAILED rate 24.83% | ✅ CONFIRMED (24.8327%) |
| 11 | node-elevated-failure-rate: 113 findings / 87 machines | ✅ CONFIRMED |
| 12 | node-hardware-fault fires once, on r216287-n200569 | ✅ CONFIRMED |
| 13 | that node: 465 jobs, 267 FAILED (57%), 0 NODE_FAIL | ✅ CONFIRMED (57.419%) |
| **14** | **all 5 drain-targeted nodes: 0 hardware-fault, 0 node-failure; 68–82% array-task-failure** | ✅ **CONFIRMED** |
| 15 | 5 nodes deliver 15,301 GPU-h = $38,252 | ⚠️ corrected, $38,252.57 → **$38,253** |
| 16 | r216287-n200569 absent from top 20 | ✅ CONFIRMED |

**Claim 14 detail as independently recomputed:**

| node | findings | hardware-fault | node-failure | array-task-failure |
|---|---|---|---|---|
| r4605940-n772143 | 259 | 0 | 0 | 209 (80.7%) |
| r7317916-n772143 | 164 | 0 | 0 | 134 (81.7%) |
| r3974592-n172107 | 162 | 0 | 0 | 122 (75.3%) |
| r4144777-n172107 | 154 | 0 | 0 | 104 (67.5%) |
| r7317916-n303509 | 136 | 0 | 0 | 95 (69.9%) |

## Corrections applied

1. **$38,252 → $38,253.** 15,301.028 × $2.50 = $38,252.57, which rounds up.
2. **`UNDECODED_1024` added to the outcome table**, one job, 0.009 GPU-h. It rounds to
   zero, which is why it was dropped, but a state table should be exhaustive.
3. Noted that $306,021 must be computed from the *unrounded* midpoint (122,408.408);
   multiplying the displayed 122,408 gives $306,020.

## Methodology criticism: accepted, and now stated on the record

The verifier's substantive objection:

> "high and low are exposure estimates based on selected detector unions, not defensible
> bounds on recoverable waste; detector overlap and unflagged jobs make them judgment
> calls."

**We agree**, and `NUMBERS.md` §1b now says so explicitly. The interval is a *scenario*
range between two policies (provably-zero-utilization at the low end, all
detector-flagged hours at the high end), not a statistical confidence interval. This is
why `claims.json` sets `interval_kind: "scenario"`, the schema offers `uncertainty` and
`scenario`, and only the latter is honest here.

It also correctly notes that ranking nodes by finding count is itself a judgment, and
that array-task failures may be workload- or array-related rather than node hardware
evidence. **That is our argument against `rec_drain_nodes`**, the verifier
independently reached our own conclusion.

## Reproducing this

```bash
docker compose up -d api
codex exec -m gpt-5.6-terra --sandbox workspace-write "$(cat analysis/VERIFY_PROMPT.md)"
```

Note: `gpt-5.4` (the default in `~/.codex/config.toml`) is rejected on a ChatGPT
account; `gpt-5.6-terra` works. The verifier could not reach `localhost:8000` from its
sandbox and computed the API-derived claims (3, 4, 14, 16) directly from
`data/synthetic/findings.json` and the resource graph instead, an *independent* path to
the same numbers, which strengthens rather than weakens the check.
