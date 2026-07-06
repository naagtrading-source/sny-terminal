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
            if not cur or cur <= 0: continue   # missing price -> skip, don't log -100%
            entry = r.get("ltp", 0)
            if not entry or entry <= 0: continue
            fwd_pct = round((cur - entry) / entry * 100, 2)
            # did price move in the signal's implied direction? use the underlying
            # bias (bullish->up expected, bearish->down expected). For option legs,
            # fwd_pct is the option's own move; correctness is judged on the option
            # going the way the label implies (buying/covering up, writing/unwind down).
            _act = r.get("activity","")
            _bull_opt = _act in ("CALL BUYING","PUT SHORT COVER","CALL SHORT COVER","LONG BUILDUP","SHORT COVERING")
            _bear_opt = _act in ("PUT BUYING","CALL WRITING","PUT WRITING","SHORT BUILDUP","LONG UNWINDING","CALL LONG UNWIND","PUT LONG UNWIND")
            if _bull_opt:   correct = fwd_pct > 0
            elif _bear_opt: correct = fwd_pct < 0
            else:           correct = None  # neutral/unclear
            f.write(json.dumps({"sig_ts": r["ts"], "horizon_min": horizon,
                "sym": r.get("sym"), "strike": r.get("strike"), "otype": r.get("otype"),
                "activity": r.get("activity"), "vol_mult": r.get("vol_mult"),
                "oi_pct": r.get("oi_pct"), "paired": r.get("paired", False),
                "entry": entry, "now": cur, "fwd_pct": fwd_pct,
                "direction_correct": correct,
                "measured_at": now.isoformat(timespec="seconds")}) + "\n")
            wrote += 1
            time.sleep(0.3)
    _measure_gold(now, done)
    print(f"[outcome] recorded {wrote} forward returns")


def _measure_gold(now, done):
    """Separate pass: measure gold_retest/gold_form signals on the UNDERLYING.
    Entry = signal price (retest) or zone mid (formation). Correct = underlying
    moved the block's direction after the horizon. Uses intraday last close."""
    import datetime as _dt
    try:
        from gold_retest import NIFTY50, INDICES
    except Exception:
        return
    # collect recent gold signals (last 130 min so a 60-min horizon can resolve)
    gold = []
    try:
        for line in open(SIG):
            try: r = json.loads(line)
            except Exception: continue
            if r.get("src") not in ("gold_retest", "gold_form"): continue
            try:
                sts = _dt.datetime.fromisoformat(r["ts"])
            except Exception: continue
            age = (now - sts).total_seconds() / 60.0
            if 55 <= age <= 130:            # ready for the 60-min horizon
                gold.append((r, age))
    except FileNotFoundError:
        return
    if not gold:
        return
    # gold-specific dedup: (sig_ts, horizon, src) from existing gold rows
    gdone = set()
    if os.path.exists(OUT):
        for line in open(OUT):
            try:
                o = json.loads(line)
                if o.get("src") in ("gold_retest", "gold_form"):
                    gdone.add((o["sig_ts"], o["horizon_min"], o["src"]))
            except Exception:
                pass
    from dhanhq import DhanContext, dhanhq
    ctx = DhanContext(os.environ["DHAN_CLIENT_ID"].strip(), os.environ["DHAN_ACCESS_TOKEN"].strip())
    dhan = dhanhq(ctx)
    to = _dt.date.today().isoformat()
    frm = (_dt.date.today() - _dt.timedelta(days=2)).isoformat()

    def _last_close(sym):
        if sym in INDICES:
            ix = INDICES[sym]; sid, seg, inst = ix["id"], ix["seg"], ix["inst"]
        elif sym in NIFTY50:
            sid, seg, inst = NIFTY50[sym], "NSE_EQ", "EQUITY"
        else:
            return 0
        try:
            r = dhan.intraday_minute_data(security_id=str(sid), exchange_segment=seg,
                                          instrument_type=inst, from_date=frm, to_date=to, interval=1)
            c = (r.get("data") or {}).get("close") or []
            return float(c[-1]) if c else 0
        except Exception:
            return 0

    import time as _t
    wrote = 0
    with open(OUT, "a") as f:
        for r, age in gold:
            horizon = 60
            if (r["ts"], horizon, r.get("src")) in gdone:
                continue
            sym = r.get("sym")
            direction = r.get("dir", 0)
            if r.get("src") == "gold_retest":
                entry = r.get("price", 0)
            else:  # formation: zone midpoint
                entry = (r.get("zone_top", 0) + r.get("zone_bot", 0)) / 2
            if not entry or entry <= 0:
                continue
            cur = _last_close(sym)
            _t.sleep(0.2)
            if not cur or cur <= 0:
                continue
            fwd_pct = round((cur - entry) / entry * 100, 2)
            correct = (fwd_pct > 0) if direction == 1 else (fwd_pct < 0) if direction == -1 else None
            f.write(json.dumps({"sig_ts": r["ts"], "horizon_min": horizon,
                "src": r.get("src"), "sym": sym, "tf": r.get("tf"),
                "dir": direction, "grade": r.get("grade"), "score": r.get("score"),
                "entry": entry, "now": cur, "fwd_pct": fwd_pct,
                "direction_correct": correct,
                "measured_at": now.isoformat(timespec="seconds")}) + "\n")
            wrote += 1
    print(f"[outcome] recorded {wrote} gold forward returns")


if __name__ == "__main__":
    main()
