"""
outcome_tracker.py — measure forward returns of logged signals.
Run periodically (e.g. every 15 min via timer). For each intraday signal in
the last ~90 min without a recorded outcome, fetch current price and append
the forward move to outcomes.jsonl. Over weeks this reveals which signal
types actually precede moves — your measured edge, per category.
"""
import os, sys, json, subprocess, datetime
BASE = os.path.dirname(os.path.abspath(__file__))
SIG = os.path.join(BASE, "signals.jsonl")
OUT = os.path.join(BASE, "outcomes.jsonl")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

def _load_dotenv():
    try:
        from dotenv import load_dotenv; load_dotenv(os.path.join(BASE, ".env"))
    except Exception: pass

def main():
    _load_dotenv()
    now = datetime.datetime.now(IST)
    # signals from the last 90 min, intraday only
    recent = []
    try:
        for line in open(SIG):
            r = json.loads(line)
            if r.get("src") != "intraday": continue
            try: t = datetime.datetime.fromisoformat(r["ts"])
            except Exception: continue
            age = (now - t).total_seconds() / 60
            if 13 <= age <= 90:  # mature enough to measure, not too old
                recent.append((r, round(age)))
    except FileNotFoundError:
        print("no signals yet"); return
    if not recent:
        print("[outcome] nothing to measure"); return
    # already-measured keys (sig ts + horizon) to avoid dupes
    done = set()
    if os.path.exists(OUT):
        for line in open(OUT):
            try:
                o = json.loads(line); done.add((o["sig_ts"], o["horizon_min"]))
            except Exception: pass
    # resolve tokens
    out = subprocess.run([sys.executable, "auth_helper.py"], capture_output=True,
                         text=True, cwd=BASE, timeout=180)
    tm = json.loads(out.stdout).get("token_map", {})
    tokmap = {}
    for cat, es in tm.items():
        for e in es:
            # log stores underlying 'sym' (e.g. HDFCBANK) -> match token_map 'symbol';
            # futures strike is None in token_map but "FUT" in the log -> normalize.
            und = (e.get("symbol") or e.get("sym", "").split("-")[0])
            strike = str(e.get("strike")) if e.get("strike") else "FUT"
            k = (und, strike, e.get("type"))
            tokmap[k] = (e.get("tok"), e.get("seg"))
    # batch fetch current prices
    from dhanhq import DhanContext, dhanhq
    ctx = DhanContext(os.environ["DHAN_CLIENT_ID"].strip(), os.environ["DHAN_ACCESS_TOKEN"].strip())
    dhan = dhanhq(ctx)
    import time
    wrote = 0
    with open(OUT, "a") as f:
        for r, age in recent:
            horizon = 15 if age < 30 else (30 if age < 60 else 60)
            if (r["ts"], horizon) in done: continue
            k = (r.get("sym"), r.get("strike"), r.get("otype"))
            tok_seg = tokmap.get(k)
            if not tok_seg or not tok_seg[0]: continue
            tok, seg = tok_seg
            cur = 0
            for _ in range(3):
                q = dhan.quote_data({seg: [int(tok)]})
                if isinstance(q, dict) and q.get("status") == "success":
                    cur = q.get("data",{}).get("data",{}).get(seg,{}).get(str(tok),{}).get("last_price", 0)
                    break
                time.sleep(1.5)
            if not cur: continue
            entry = r.get("ltp", 0)
            if not entry: continue
            fwd_pct = round((cur - entry) / entry * 100, 2)
            # did price move in the signal's implied direction?
            bias_up = (r.get("activity","") in ("LONG BUILDUP","CALL BUYING","SHORT COVERING","PUT SHORT COVER")
                       or (r.get("side")=="BUYING"))
            correct = (fwd_pct > 0) == bias_up
            f.write(json.dumps({"sig_ts": r["ts"], "horizon_min": horizon,
                "sym": r.get("sym"), "strike": r.get("strike"), "otype": r.get("otype"),
                "activity": r.get("activity"), "vol_mult": r.get("vol_mult"),
                "oi_pct": r.get("oi_pct"), "paired": r.get("paired", False),
                "entry": entry, "now": cur, "fwd_pct": fwd_pct,
                "direction_correct": correct,
                "measured_at": now.isoformat(timespec="seconds")}) + "\n")
            wrote += 1
            time.sleep(0.3)
    print(f"[outcome] recorded {wrote} forward returns")

if __name__ == "__main__":
    main()
