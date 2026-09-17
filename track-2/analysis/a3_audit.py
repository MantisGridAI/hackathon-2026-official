"""A3 — audit the API's own rec_drain_nodes recommendation.

api/main.py:571-580 recommends draining the top-5 nodes by FINDING COUNT.
api/main.py:511-516 admits the ranking "does not read rootCauses, so correlated
findings sharing one cause are counted as independent problems."

docs/rules.md on array-task-failure: "The machines are innocent... if a node were
at fault the failures would concentrate on it. They share an exit code instead."

So: does rec_drain_nodes recommend draining healthy machines? Check every finding
it cites against causal(), and price the error.
"""
import json
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "starter"))
from mgai_client import MGAI  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
mg = MGAI()
jobs = pd.read_parquet(ROOT / "data/prepped/jobs.parquet")
gpus = pd.read_parquet(ROOT / "data/prepped/gpus.parquet")
PRICE = mg.price_book()["usd_per_gpu_hour"]

print("\n" + "=" * 78)
print("WHAT THE API RECOMMENDS")
print("=" * 78)
recs = mg.recommendations()
drain = next(r for r in recs if r["id"] == "rec_drain_nodes")
print(f"  title      {drain['title']}")
print(f"  action     {drain['action']}")
print(f"  savings    ${drain['estimated_savings']['amount']:,.0f} "
      f"({drain['estimated_savings_gpu_hours']:,.0f} GPU-h)")
print(f"  effort     {drain['effort']}        confidence {drain['confidence']}")
print(f"  kind       {drain['kind']}")
print(f"  cites      {len(drain['finding_ids'])} findings")

# Which nodes does it actually name? underperforming(node) is the same ranking.
under = mg.underperforming(entity_type="node", limit=5)
named = [r["entity_id"] for r in under["rows"]]
print(f"\n  ranked nodes (by finding count, per the endpoint's own method):")
for r in under["rows"]:
    print(f"    {r['entity_id']:<24} {r['finding_count']:>4} findings  "
          f"{r['impact_gpu_hours']:>10,.0f} GPU-h")
print(f"\n  endpoint caveat: {under['provenance'].get('caveat')}")

# ------------------------------------------------------------------ audit
print("\n" + "=" * 78)
print("WHAT causal() SAYS IS ACTUALLY UNDERNEATH THOSE FINDINGS")
print("=" * 78)

verdicts = Counter()
detail = []
for fid in drain["finding_ids"]:
    ch = mg.causal(fid)
    if not ch:
        verdicts["no_causal_chain"] += 1
        detail.append((fid, "no_chain", None, None))
        continue
    top = ch[0]["culprit"][0]
    verdicts[top["type"]] += 1
    detail.append((fid, top["type"], top.get("node"), ch[0].get("confidence")))

print(f"  audited {len(drain['finding_ids'])} cited findings:")
for t, n in verdicts.most_common():
    print(f"    {t:<34} {n:>4}")

nonnode = sum(n for t, n in verdicts.items()
              if t not in ("k8s:node", "no_causal_chain"))
print(f"\n  -> {nonnode} of {len(drain['finding_ids'])} cited findings resolve to "
      f"something OTHER than a machine")

# ------------------------------------------------- per-node, was draining right?
print("\n" + "=" * 78)
print("PER-NODE VERDICT  (is draining this machine justified?)")
print("=" * 78)

all_f = mg.findings_df()
all_f["node"] = all_f.get("metadata_node")
all_f["job_id"] = pd.to_numeric(all_f.get("metadata_job_id"), errors="coerce")

# Real delivered hours per node, from gpus.parquet (NOT primary_node).
node_hours = gpus.groupby("Node").gpu_hours.sum()

rows = []
for node in named:
    nf = all_f[all_f.node == node]
    by_det = nf.detectorId.value_counts()

    # Do this node's findings resolve to the node, or elsewhere?
    causes = Counter()
    for fid in nf.id.head(25):
        ch = mg.causal(fid)
        if ch:
            causes[ch[0]["culprit"][0]["type"]] += 1
        else:
            causes["no_chain"] += 1

    hw = int(by_det.get("rules::node-hardware-fault", 0))
    nodefail = int(by_det.get("rules::node-failure", 0))
    arrays = int(by_det.get("rules::array-task-failure", 0))
    delivered = float(node_hours.get(node, 0.0))

    # Owner concentration among this node's FAILED jobs -- the user_code test.
    jn = gpus[gpus.Node == node].id_job.unique()
    jf = jobs[jobs.id_job.isin(jn) & (jobs.state_name == "FAILED")]
    if len(jf):
        top_user_share = jf.id_user.value_counts(normalize=True).iloc[0]
        n_users = jf.id_user.nunique()
    else:
        top_user_share, n_users = 0.0, 0

    justified = hw > 0
    rows.append(dict(
        node=node, findings=int(len(nf)), hardware_fault=hw, node_failures=nodefail,
        array_task_findings=arrays, delivered_gpu_hours=round(delivered, 1),
        failed_jobs=int(len(jf)), distinct_users_failing=n_users,
        top_user_share_of_failures=round(float(top_user_share), 3),
        causal_types=dict(causes), drain_justified=justified,
    ))

    print(f"\n  {node}")
    print(f"    findings                {len(nf)}")
    print(f"    node-hardware-fault     {hw}      <-- the only scheduler-backed "
          f"hardware evidence")
    print(f"    node-failure            {nodefail}")
    print(f"    array-task-failure      {arrays}      (cause is the ARRAY, not the machine)")
    print(f"    delivered               {delivered:,.0f} GPU-h  "
          f"(${delivered * PRICE:,.0f} of capacity)")
    print(f"    failed jobs             {len(jf)} across {n_users} users; "
          f"top user owns {top_user_share:.0%}")
    print(f"    causal() resolves to    {dict(causes)}")
    print(f"    DRAIN JUSTIFIED?        {'YES' if justified else 'NO'}")

df = pd.DataFrame(rows)
unjust = df[~df.drain_justified]
cap_at_risk = float(unjust.delivered_gpu_hours.sum())

print("\n" + "=" * 78)
print("THE COST OF FOLLOWING THIS RECOMMENDATION")
print("=" * 78)
print(f"  nodes recommended for draining        {len(df)}")
print(f"  with scheduler-backed hardware fault  {int(df.drain_justified.sum())}")
print(f"  with NO hardware evidence             {len(unjust)}")
print(f"\n  capacity destroyed if we drain the unjustified ones:")
print(f"    {cap_at_risk:,.0f} GPU-hours  =  ${cap_at_risk * PRICE:,.0f}")
print(f"  claimed savings from the recommendation:")
print(f"    {drain['estimated_savings_gpu_hours']:,.0f} GPU-hours  "
      f"=  ${drain['estimated_savings']['amount']:,.0f}")
ratio = cap_at_risk / max(drain["estimated_savings_gpu_hours"], 1)
print(f"\n  -> we would destroy {ratio:.1f}x more capacity than we recover")

out = dict(
    recommendation=dict(
        id=drain["id"], title=drain["title"], action=drain["action"],
        claimed_savings_usd=drain["estimated_savings"]["amount"],
        claimed_savings_gpu_hours=drain["estimated_savings_gpu_hours"],
        confidence=drain["confidence"], effort=drain["effort"], kind=drain["kind"],
        n_cited_findings=len(drain["finding_ids"]),
    ),
    cited_finding_causal_types=dict(verdicts),
    cited_findings_not_resolving_to_node=nonnode,
    per_node=rows,
    nodes_recommended=len(df),
    nodes_with_hardware_evidence=int(df.drain_justified.sum()),
    nodes_without_hardware_evidence=int(len(unjust)),
    capacity_at_risk_gpu_hours=round(cap_at_risk, 1),
    capacity_at_risk_usd=round(cap_at_risk * PRICE, 2),
    destroy_to_recover_ratio=round(ratio, 2),
    price_per_gpu_hour=PRICE,
)
(ROOT / "analysis/a3_audit.json").write_text(json.dumps(out, indent=2, default=str))
print(f"\n  written -> analysis/a3_audit.json\n")
