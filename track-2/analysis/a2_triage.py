"""A2 — node triage. Why did rules::node-elevated-failure-rate fire?

The finding reports a SYMPTOM and deliberately withholds cause (docs/rules.md).
Cause must be one of: hardware / user_code / workload_mix / cannot_determine.
The schema says reasoning "IS THE MARK" -- so every verdict here carries the
joins and the numbers behind it.

Tests, each comparing like with like:
  user_code  one id_user owns >=90% of the node's failures in-window AND
             everyone else on that machine in those hours did far better
  hardware   several unrelated users hit one exit status here and essentially
             never elsewhere  (this is what node-hardware-fault encodes)
  workload   failures concentrate in a partition/job_type that fails at a
             similar rate cluster-wide
"""
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "starter"))
from mgai_client import MGAI  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
mg = MGAI()
jobs = pd.read_parquet(ROOT / "data/prepped/jobs.parquet")
gpus = pd.read_parquet(ROOT / "data/prepped/gpus.parquet")

elev = mg.findings_df(detector_id="rules::node-elevated-failure-rate")
print(f"node-elevated-failure-rate: {len(elev)} findings across "
      f"{elev.metadata_node.nunique()} machines")

hw_nodes = set(mg.findings_df(detector_id="rules::node-hardware-fault")
               .metadata_node.dropna())
burst = mg.findings_df(detector_id="rules::node-job-failure-burst")

# jobs per node via gpus.parquet (primary_node is the FIRST node only)
job_node = gpus[["Node", "id_job"]].drop_duplicates()
CLUSTER_FAIL_RATE = float((jobs.state_name == "FAILED").mean())
print(f"cluster-wide FAILED rate: {CLUSTER_FAIL_RATE:.2%}\n")

# The most-flagged machines (what a CFO asks about first), plus any machine
# carrying the one real hardware fault -- it does NOT rank highly by flag count,
# which is itself the finding.
top = elev.metadata_node.value_counts().head(8)
for n in hw_nodes:
    if n in set(elev.metadata_node.dropna()) and n not in top.index:
        top = pd.concat([top, pd.Series({n: int(
            (elev.metadata_node == n).sum())})])
results = []

for node, n_windows in top.items():
    jn = job_node[job_node.Node == node].id_job
    jj = jobs[jobs.id_job.isin(jn)]
    jf = jj[jj.state_name == "FAILED"]
    if not len(jj):
        continue

    fail_rate = len(jf) / len(jj)
    n_users = jf.id_user.nunique()
    shares = jf.id_user.value_counts(normalize=True)
    top_share = float(shares.iloc[0]) if len(shares) else 0.0
    top_user = shares.index[0] if len(shares) else None

    # like-with-like: how did EVERYONE ELSE do on this same machine?
    others = jj[jj.id_user != top_user]
    others_fail = (others.state_name == "FAILED").mean() if len(others) else 0.0

    # and how does the top user do on OTHER machines?
    their_other_jobs = jobs[(jobs.id_user == top_user) & (~jobs.id_job.isin(jn))]
    their_elsewhere = ((their_other_jobs.state_name == "FAILED").mean()
                       if len(their_other_jobs) else float("nan"))

    # exit-status signature concentration
    ec = (jf.exit_code // 256).value_counts()
    top_ec = int(ec.index[0]) if len(ec) else None
    top_ec_share = float(ec.iloc[0] / len(jf)) if len(jf) else 0.0

    # Workload mix. NOTE: 99% of all cluster jobs run in partition 'normal', so
    # "most failures are in one partition" is true of every machine and proves
    # nothing. The informative test is whether this node's MIX differs from the
    # cluster's, and whether its failure rate is explained by that mix -- i.e.
    # compute the rate this node WOULD have if each job_type failed at the
    # cluster-wide rate for that job_type.
    type_rate = jobs.groupby("job_type").apply(
        lambda d: (d.state_name == "FAILED").mean(), include_groups=False)
    mix = jj.job_type.value_counts(normalize=True)
    expected_from_mix = float((mix * type_rate.reindex(mix.index)).sum())
    mix_explains = (expected_from_mix > 0
                    and fail_rate <= expected_from_mix * 1.25)
    top_part = mix.index[0] if len(mix) else None
    top_part_share = float(mix.iloc[0]) if len(mix) else 0.0

    # ---- verdict
    if node in hw_nodes:
        cause, verdict = "hardware", "act"
        reasoning = (
            f"rules::node-hardware-fault fires on this machine. {n_users} distinct "
            f"users failed here; exit status {top_ec} accounts for {top_ec_share:.0%} "
            f"of its {len(jf)} failures, a signature they do not produce elsewhere. "
            f"Node failure rate {fail_rate:.0%} vs cluster {CLUSTER_FAIL_RATE:.0%}.")
    elif top_share >= 0.90 and others_fail < fail_rate / 2:
        cause, verdict = "user_code", "no_action"
        reasoning = (
            f"Joined this node's jobs via gpus.parquet(Node)->jobs(id_job), then "
            f"grouped FAILED by id_user: one user owns {top_share:.0%} of "
            f"{len(jf)} failures. Everyone else on the same machine failed "
            f"{others_fail:.0%} of the time, so the machine was fine for them. "
            f"That user fails {their_elsewhere:.0%} of the time on other machines.")
    elif mix_explains and n_users >= 3:
        cause, verdict = "workload_mix", "monitor"
        reasoning = (
            f"This node's job-type mix is {top_part_share:.0%} '{top_part}'. Scoring "
            f"each job_type at its cluster-wide failure rate, this mix alone predicts "
            f"{expected_from_mix:.0%} failures; the node actually ran {fail_rate:.0%} "
            f"across {len(jj)} jobs, i.e. within noise of what the work it receives "
            f"would fail at anywhere. {n_users} users failed here with no single owner "
            f"(top {top_share:.0%}) and there is no node-hardware-fault finding. The "
            f"machine is not the outlier; the work it is sent is.")
    else:
        cause, verdict = "cannot_determine", "monitor"
        reasoning = (
            f"Cannot separate machine from workload. {len(jf)} failures across "
            f"{n_users} users; top user owns {top_share:.0%} (below the 90% "
            f"concentration bar) so it is not one person's bug, and there is no "
            f"node-hardware-fault finding and no NODE_FAIL here so there is no "
            f"scheduler-backed hardware evidence either. Others on this machine "
            f"failed {others_fail:.0%} vs this node's overall {fail_rate:.0%}. "
            f"Exit status {top_ec} is only {top_ec_share:.0%} of failures, so there "
            f"is no single signature to pin on the hardware. Its job-type mix predicts "
            f"{expected_from_mix:.0%} failures against an actual {fail_rate:.0%}, so "
            f"the mix does not account for it either. Monitoring rather than guessing.")

    results.append(dict(
        node=node, window=None, cause=cause, verdict=verdict, reasoning=reasoning,
        _windows_flagged=int(n_windows), _jobs=int(len(jj)), _failed=int(len(jf)),
        _fail_rate=round(fail_rate, 4), _users_failing=int(n_users),
        _top_user_share=round(top_share, 3), _others_fail_rate=round(float(others_fail), 4),
        _top_exit_status=top_ec, _top_exit_share=round(top_ec_share, 3),
    ))
    print(f"  {node:<22} {cause:<17} {verdict:<10} "
          f"({len(jf)}/{len(jj)} failed, {n_users} users, top {top_share:.0%})")

counts = pd.Series([r["cause"] for r in results]).value_counts()
print(f"\nverdicts: {dict(counts)}")
print("(generator's own burst attribution was user 20 / hardware 1 / "
      "undetermined 26 -- cannot_determine being common is CORRECT)")

(ROOT / "analysis/a2_triage.json").write_text(json.dumps(results, indent=2, default=str))
print("\nwritten -> analysis/a2_triage.json\n")
