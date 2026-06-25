"""
quote_helper.py — FAST quote refresher subprocess.
Reads a token list from stdin, logs in, fetches ONLY those quotes (no scrip
search / no option discovery — that's the slow part), prints JSON, exits.
This keeps quotes live-updating without re-running the heavy token discovery.
"""
import os, sys, json, io, contextlib, threading

def _silent(fn, timeout=5):
    r=[None]; e=[None]
    def w():
        buf=io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                r[0]=fn()
        except Exception as ex: e[0]=ex
    t=threading.Thread(target=w,daemon=True); t.start(); t.join(timeout=timeout)
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
    if not tokens:
        print(json.dumps({"error":"empty_tokens"})); return

    from neo_api_client import NeoAPI

    ck   = os.environ["KOTAK_CONSUMER_KEY"]
    mob  = os.environ["KOTAK_MOBILE"].lstrip("+").lstrip("91") if os.environ.get("KOTAK_MOBILE") else ""
    mob  = mob[-10:]
    ucc  = os.environ.get("KOTAK_UCC","")
    mpin = os.environ.get("KOTAK_MPIN","")
    import pyotp
    totp = pyotp.TOTP(os.environ["KOTAK_TOTP_SECRET"]).now()

    api=_silent(lambda: NeoAPI(environment="prod",consumer_key=ck))
    ok=False
    for mfmt in [f"+91{mob}",mob,f"91{mob}"]:
        r1=_silent(lambda m=mfmt: api.totp_login(mobile_number=m,ucc=ucc,totp=totp))
        if isinstance(r1,dict) and not r1.get("error"):
            ok=True; break
    if not ok:
        print(json.dumps({"error":"login_failed"})); return
    _silent(lambda: api.totp_validate(mpin=mpin))

    # Fetch quotes for the known tokens — batched where possible
    quotes={}
    for t in tokens:
        tk=str(t["tok"]); seg=t["seg"]
        try:
            qr=_silent(lambda tk=tk,seg=seg: api.quotes(
                instrument_tokens=[{"instrument_token":tk,"exchange_segment":seg}],
                quote_type=None))
            q=_extract_quote(qr)
            if q: quotes[tk]=q
        except Exception:
            pass

    print(json.dumps({"quotes":quotes}))
    sys.stdout.flush()

if __name__=="__main__":
    main()
