"""outcome_summary.py -- read outcomes.jsonl and report which signal types work."""
import os, json, collections
BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "outcomes.jsonl")

def _load():
    rows = []
    if not os.path.exists(OUT):
        return rows
    for line in open(OUT):
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows

def _stat(group):
    judged = [r for r in group if r.get("direction_correct") is not None]
    n = len(judged)
    if n == 0:
        return (len(group), None, None)
    hits = sum(1 for r in judged if r["direction_correct"])
    avg = sum(r.get("fwd_pct", 0) for r in judged) / n
    return (n, hits / n * 100, avg)

def _print_table(title, buckets):
    print(f"\n=== {title} ===")
    print(f"{'group':<28}{'n':>5}{'hit%':>8}{'avg fwd%':>11}")
    print("-" * 52)
    def _key(item):
        n, hr, avg = item[1]
        return (-(hr if hr is not None else -1), -n)
    for name, s in sorted(buckets.items(), key=_key):
        n, hr, avg = s
        hr_s = f"{hr:.0f}%" if hr is not None else "  -"
        avg_s = f"{avg:+.2f}%" if avg is not None else "   -"
        print(f"{str(name):<28}{n:>5}{hr_s:>8}{avg_s:>11}")

def main():
    rows = _load()
    if not rows:
        print("no outcomes yet"); return
    gold = [r for r in rows if str(r.get("src", "")).startswith("gold")]
    opts = [r for r in rows if not str(r.get("src", "")).startswith("gold")]
    print(f"total outcomes: {len(rows)}  |  gold: {len(gold)}  |  options/futures: {len(opts)}")
    if opts:
        by_act = collections.defaultdict(list)
        for r in opts:
            by_act[r.get("activity", "?")].append(r)
        _print_table("Options/Futures by activity", {k: _stat(v) for k, v in by_act.items()})
        by_vol = collections.defaultdict(list)
        for r in opts:
            vm = r.get("vol_mult", 0) or 0
            tier = "vol >=10x" if vm >= 10 else "vol 5-10x" if vm >= 5 else "vol <5x (weak)"
            by_vol[tier].append(r)
        _print_table("Options/Futures by volume tier", {k: _stat(v) for k, v in by_vol.items()})
    if gold:
        by_src = collections.defaultdict(list)
        for r in gold:
            by_src[r.get("src", "?")].append(r)
        _print_table("Gold by type", {k: _stat(v) for k, v in by_src.items()})
        by_grade = collections.defaultdict(list)
        for r in gold:
            by_grade[r.get("grade", "?")].append(r)
        _print_table("Gold by grade", {k: _stat(v) for k, v in by_grade.items()})
        by_score = collections.defaultdict(list)
        for r in gold:
            sc = r.get("score", 0) or 0
            tier = "score 100" if sc >= 100 else "score 85-99" if sc >= 85 else "score 75-84"
            by_score[tier].append(r)
        _print_table("Gold by score tier", {k: _stat(v) for k, v in by_score.items()})
    print("\nnote: hit% needs 30+ per group before trusting. small n = noise.")

if __name__ == "__main__":
    main()
