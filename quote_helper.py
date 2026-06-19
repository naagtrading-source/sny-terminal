"""
quote_helper.py — FAST quote fetcher. Reuses the pickled SDK session saved by
auth_helper.py, so it skips the slow login. Reads token list from stdin,
fetches live quotes, prints JSON. Runs in ~3-5s instead of ~60s.
"""
import sys, json, io, contextlib, threading, pickle, os

def _silent(fn):
    r=[None]; e=[None]
    def w():
        buf=io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                r[0]=fn()
        except Exception as ex: e[0]=ex
    t=threading.Thread(target=w,daemon=True); t.start(); t.join(timeout=25)
    if e[0]: raise e[0]
    return r[0]

def _extract_quote(qr):
    if qr is None: return {}
    if isinstance(qr,list): return qr[0] if qr and isinstance(qr[0],dict) else {}
    if isinstance(qr,dict):
        for dk in ("data","Data","result","Result"):
            if dk in qr:
                inn=qr[dk]
                if isinstance(inn,list) and inn: return inn[0] if isinstance(inn[0],dict) else {}
                if isinstance(inn,dict): return inn
        return qr
    return {}

def main():
    raw_in=sys.stdin.read().strip()
    if not raw_in:
        print(json.dumps({"error":"no_input"})); return
    tokens=json.loads(raw_in)   # [{"tok":"x","seg":"y"}, ...]

    # Restore the pickled session (no login!)
    if not os.path.exists("/tmp/kotak_api.pkl"):
        print(json.dumps({"error":"no_session"})); return
    try:
        with open("/tmp/kotak_api.pkl","rb") as sf:
            api=pickle.load(sf)
    except Exception as ex:
        print(json.dumps({"error":f"session_load:{ex}"})); return

    quotes={}
    for t in tokens:
        tk=str(t["tok"]); seg=t["seg"]
        try:
            qr=_silent(lambda tk=tk,seg=seg: api.quotes(
                instrument_tokens=[{"instrument_token":tk,"exchange_segment":seg}],
                quote_type=None))
            q=_extract_quote(qr)
            if q: quotes[tk]=q
        except Exception as ex:
            print(f"[quote] {tk} err: {ex}", file=sys.stderr, flush=True)

    print(json.dumps({"quotes":quotes}))
    sys.stdout.flush()

if __name__=="__main__":
    main()
