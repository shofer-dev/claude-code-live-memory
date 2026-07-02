#!/usr/bin/env python3
"""Aggregate 3 reps of the hybrid A/B. Reports per-task adoption rate (with-arm reps
that actually called ask_live_memory), mean read_tok / premium$ / turns per arm, and
pass rate. Separates signal (adopted) from noise (not adopted)."""
import csv, glob, statistics as st
from collections import defaultdict

reps = sorted(glob.glob("/tmp/pilot/hybrid_r*/results.csv"))
print("reps:", reps)
# rows[(id,arm)] = list of dict per rep
rows = defaultdict(list)
meta = {}
for f in reps:
    for r in csv.DictReader(open(f)):
        k = (r["id"], r["arm"])
        rows[k].append(r)
        meta[r["id"]] = r["type"]

def col(rs, c, cast=float): return [cast(x[c]) for x in rs]
ids = ["bug1", "bug2", "feat1", "feat2"]

print(f"\n{'task':7}{'type':8}{'arm':8}{'pass':>7}{'adopt':>7}{'read_tok':>10}{'turns':>7}{'prem$':>9}")
agg = {}
for tid in ids:
    for arm in ("without", "with"):
        rs = rows[(tid, arm)]
        if not rs: continue
        passes = sum(x["passed"] == "True" for x in rs)
        adopt = sum(int(x["lm_calls"]) > 0 for x in rs) if arm == "with" else 0
        rt = st.mean(col(rs, "read_tok")); tn = st.mean(col(rs, "turns")); pr = st.mean(col(rs, "prem_usd"))
        agg[(tid, arm)] = dict(rt=rt, pr=pr, tn=tn, passes=passes, n=len(rs), adopt=adopt)
        adm = f"{adopt}/{len(rs)}" if arm == "with" else "-"
        print(f"{tid:7}{meta[tid]:8}{arm:8}{passes}/{len(rs):>4}{adm:>7}{rt:>10.0f}{tn:>7.1f}{pr:>9.4f}")

def pct(a, b): return 100 * (b - a) / a if a else 0
print("\n=== per-task with-vs-without (mean over reps) ===")
for tid in ids:
    a, b = agg[(tid, "without")], agg[(tid, "with")]
    print(f"{tid:7} read_tok {a['rt']:6.0f}->{b['rt']:6.0f} ({pct(a['rt'],b['rt']):+5.0f}%)  "
          f"prem$ {a['pr']:.3f}->{b['pr']:.3f} ({pct(a['pr'],b['pr']):+5.0f}%)  "
          f"turns {a['tn']:.1f}->{b['tn']:.1f}  adopt {b['adopt']}/{b['n']}")

# cumulative (sum of per-task means)
wo_rt = sum(agg[(t, "without")]["rt"] for t in ids); wi_rt = sum(agg[(t, "with")]["rt"] for t in ids)
wo_pr = sum(agg[(t, "without")]["pr"] for t in ids); wi_pr = sum(agg[(t, "with")]["pr"] for t in ids)
tot_adopt = sum(agg[(t, "with")]["adopt"] for t in ids); tot_n = sum(agg[(t, "with")]["n"] for t in ids)
print(f"\nCUMULATIVE (sum of per-task means):")
print(f"  read_tok  {wo_rt:.0f} -> {wi_rt:.0f}  ({pct(wo_rt,wi_rt):+.0f}%)")
print(f"  premium$  {wo_pr:.3f} -> {wi_pr:.3f}  ({pct(wo_pr,wi_pr):+.0f}%)")
print(f"  adoption (with-arm, all task-reps): {tot_adopt}/{tot_n}")

# conditional: adopted-only tasks
print("\n=== CONDITIONAL: tasks where the agent adopted memory in >=1 rep ===")
for tid in ids:
    b = agg[(tid, "with")]
    if b["adopt"] > 0:
        a = agg[(tid, "without")]
        print(f"  {tid}: prem$ {pct(a['pr'],b['pr']):+.0f}%  read_tok {pct(a['rt'],b['rt']):+.0f}%  turns {a['tn']:.0f}->{b['tn']:.0f}")
