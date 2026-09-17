"""A1 — the headline recoverable number, computed defensibly.

Run: python analysis/a1_headline.py
Needs: data/prepped/*.parquet and the API on :8000.
"""
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "starter"))
from mgai_client import MGAI  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CEILING = 594_004  # GPU-hours allocated. Nothing may exceed this.

jobs = pd.read_parquet(ROOT / "data/prepped/jobs.parquet")
gpus = pd.read_parquet(ROOT / "data/prepped/gpus.parquet")
mg = MGAI()

out = {}


def show(label, val, unit="GPU-h"):
    pct = f"{val / CEILING:6.2%} of cluster" if unit == "GPU-h" else ""
    print(f"  {label:<44} {val:>12,.0f} {unit}  {pct}")
    return val


print("\n" + "=" * 78)
print("GROUND TRUTH")
print("=" * 78)
allocated = show("allocated (measured DCGM)", jobs.gpu_hours.sum())
assert abs(allocated - CEILING) < 50, f"expected ~{CEILING}, got {allocated}"

by_state = jobs.groupby("state_name").gpu_hours.sum().sort_values(ascending=False)
print("\n  by outcome:")
for s, v in by_state.items():
    print(f"    {s:<42} {v:>12,.0f}  {v / allocated:6.2%}")
out["by_state"] = {k: round(float(v), 1) for k, v in by_state.items()}

# ---------------------------------------------------------------- findings
print("\n" + "=" * 78)
print("WHY THE NAIVE TOTAL IS IMPOSSIBLE")
print("=" * 78)
f = mg.findings_df()
f["impact_gpu_hours"] = pd.to_numeric(
    f.get("metadata_impact_gpu_hours"), errors="coerce")
f["scope"] = f.get("metadata_impact_scope")
f["kind"] = f.get("metadata_impact_kind")
f["job_id"] = pd.to_numeric(f.get("metadata_job_id"), errors="coerce")

naive = show("naive sum of every finding", f.impact_gpu_hours.sum())
jobscope = f[f.scope == "job"]
js_sum = show("job-scope findings only", jobscope.impact_gpu_hours.sum())

# Deduplicate: one row per job, then take the JOB's real hours (not the
# finding's slice) -- findings overlap, jobs do not.
flagged = jobscope.dropna(subset=["job_id"]).job_id.astype("int64").unique()
real = jobs[jobs.id_job.isin(flagged)]
dedup = show(f"deduplicated to {len(flagged):,} unique flagged jobs", real.gpu_hours.sum())

multi = jobscope.dropna(subset=["job_id"]).groupby("job_id").size()
print(f"\n  {len(flagged):,} distinct jobs carry a job-scope finding")
print(f"  {(multi > 1).sum():,} of them ({(multi > 1).mean():.1%}) carry MORE THAN ONE")
print(f"  -> that overlap is why the naive sum reaches {naive / CEILING:.0%} of a cluster")
print("     that only ever allocated 594,004 GPU-hours.")

out["naive_sum"] = round(float(naive), 1)
out["jobscope_sum"] = round(float(js_sum), 1)
out["dedup_flagged_job_hours"] = round(float(dedup), 1)
out["n_flagged_jobs"] = int(len(flagged))
out["n_multi_finding_jobs"] = int((multi > 1).sum())

# ---------------------------------------------------------------- by kind
print("\n" + "=" * 78)
print("DEDUPLICATED HOURS BY IMPACT KIND  (never sum across these)")
print("=" * 78)
# Assign each flagged job ONE kind, worst-first, so no job is counted twice.
PRIORITY = ["lost", "consumed", "unused_capacity", "degraded"]
jk = (jobscope.dropna(subset=["job_id"])
      .assign(job_id=lambda d: d.job_id.astype("int64"))
      [["job_id", "kind"]].drop_duplicates())
jk["rank"] = jk.kind.map({k: i for i, k in enumerate(PRIORITY)}).fillna(99)
primary = jk.sort_values("rank").drop_duplicates("job_id", keep="first")
hrs = jobs.set_index("id_job").gpu_hours
primary["gpu_hours"] = primary.job_id.map(hrs)

kind_tot = primary.groupby("kind").gpu_hours.sum().sort_values(ascending=False)
for k, v in kind_tot.items():
    show(f"  {k}", v)
print(f"  {'':<44} {'-' * 12}")
show("  TOTAL (each job counted once)", kind_tot.sum())
out["by_kind_dedup"] = {k: round(float(v), 1) for k, v in kind_tot.items()}

# ---------------------------------------------------------------- cancelled
print("\n" + "=" * 78)
print("THE CANCELLED DECISION  (swings the headline ~2x)")
print("=" * 78)
canc = float(by_state.get("CANCELLED", 0))
show("all CANCELLED hours", canc)

slow = mg.findings_df(detector_id="rules::slow-cancel-of-idle-job")
slow_jobs = pd.to_numeric(slow.metadata_job_id, errors="coerce").dropna().astype("int64")
slow_hrs = float(jobs[jobs.id_job.isin(slow_jobs)].gpu_hours.sum())
show(f"of which slow-cancel-of-idle ({len(slow_jobs)} jobs)", slow_hrs)
print(f"\n  -> {slow_hrs:,.0f} GPU-h sat idle >4h before anyone killed it.")
print("     That is a DETECTION-LATENCY problem, not a cancellation problem:")
print("     the cancel was correct, noticing late is what cost money.")
print(f"  -> the other {canc - slow_hrs:,.0f} GPU-h is users correctly")
print("     stopping runs that looked wrong. Not waste.")
out["cancelled_total"] = round(canc, 1)
out["cancelled_slow_cancel_hours"] = round(slow_hrs, 1)

# ---------------------------------------------------------------- recoverable
print("\n" + "=" * 78)
print("RECOVERABLE  (our claim)")
print("=" * 78)
never = mg.findings_df(detector_id="rules::gpu-never-computed")
notneeded = mg.findings_df(detector_id="rules::gpu-not-needed")
idle = mg.findings_df(detector_id="rules::idle-interactive-session")
lowutil = mg.findings_df(detector_id="rules::gpu-low-utilization")


def job_hours(df):
    if df.empty:
        return set()
    ids = pd.to_numeric(df.metadata_job_id, errors="coerce").dropna().astype("int64")
    return set(ids)


# Union of job ids, so overlapping rules cannot double-count.
addressable_ids = (job_hours(never) | job_hours(notneeded)
                   | job_hours(idle) | job_hours(lowutil) | set(slow_jobs))
addr = jobs[jobs.id_job.isin(addressable_ids)]
addr_hrs = float(addr.gpu_hours.sum())

print(f"  Union of 5 policy-actionable rules: {len(addressable_ids):,} unique jobs")
print("    gpu-never-computed, gpu-not-needed, idle-interactive-session,")
print("    gpu-low-utilization, slow-cancel-of-idle-job")
show("  addressable (dedup, each job once)", addr_hrs)

# Scenario interval: the recoverable fraction is a policy question, not a
# measurement. Low = only the unambiguous never-computed cases. High = all
# addressable hours. Point = a defensible middle.
unambig = jobs[jobs.id_job.isin(job_hours(never) | job_hours(notneeded))]
low = float(unambig.gpu_hours.sum())
high = addr_hrs
point = low + 0.5 * (high - low)

print()
show("  LOW  (never-computed + not-needed only)", low)
show("  POINT (50% of the contested middle)", point)
show("  HIGH (every addressable hour)", high)
print(f"\n  sanity: high {high:,.0f} <= ceiling {CEILING:,} ? "
      f"{'OK' if high <= CEILING else 'IMPOSSIBLE'}")
assert high <= CEILING

PRICE = mg.price_book()["usd_per_gpu_hour"]
print(f"\n  at ${PRICE}/GPU-hour:")
for lbl, v in [("low", low), ("point", point), ("high", high)]:
    print(f"    {lbl:<8} ${v * PRICE:>14,.0f}")

out["recoverable"] = {
    "low": round(low, 1), "point": round(point, 1), "high": round(high, 1),
    "usd_low": round(low * PRICE, 2), "usd_point": round(point * PRICE, 2),
    "usd_high": round(high * PRICE, 2), "price_per_gpu_hour": PRICE,
    "n_jobs": int(len(addressable_ids)),
}

(ROOT / "analysis").mkdir(exist_ok=True)
(ROOT / "analysis/a1_numbers.json").write_text(json.dumps(out, indent=2))
print(f"\n  written -> analysis/a1_numbers.json\n")
