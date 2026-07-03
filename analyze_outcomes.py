"""Run after ~2 weeks of data: shows hit rate + avg forward move per signal
type. Signal types with high n, hit% >60, and positive avg fwd% have edge."""
import json, collections, os
BASE = os.path.dirname(os.path.abspath(__file__))
try:
    rows = [json.loads(l) for l in open(os.path.join(BASE, "outcomes.jsonl"))]
except FileNotFoundError:
    print("No outcomes.jsonl yet — let the tracker collect first."); raise SystemExit
measured = [r for r in rows if r.get("direction_correct") is not None]
print(f"total outcomes: {len(rows)} | measurable: {len(measured)}\n")
if len(measured) < 30:
    print(f"Only {len(measured)} measured — need ~100+ for meaningful stats. Wait longer.\n")
by = collections.defaultdict(list)
for r in measured: by[r["activity"]].append(r)
print(f"{'signal type':<20}{'n':>5}{'hit%':>7}{'avg fwd%':>10}")
print("-"*42)
for act, rs in sorted(by.items(), key=lambda x: -len(x[1])):
    n=len(rs); hit=sum(1 for r in rs if r["direction_correct"])/n*100
    avg=sum(r["fwd_pct"] for r in rs)/n
    edge = "  ← edge" if (n>=20 and hit>=60 and avg>0) else ""
    print(f"{act:<20}{n:>5}{hit:>6.0f}%{avg:>+9.2f}%{edge}")
paired=[r for r in measured if r.get("paired")]
if paired:
    ph=sum(1 for r in paired if r["direction_correct"])/len(paired)*100
    pa=sum(r["fwd_pct"] for r in paired)/len(paired)
    print(f"\n🎯 BOTH-LEGS: n={len(paired)}  hit={ph:.0f}%  avg={pa:+.2f}%")
print("\nRead: signal types marked '← edge' (n>=20, hit>=60%, +avg) are worth trusting.")
