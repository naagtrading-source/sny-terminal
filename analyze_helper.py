"""
analyze_helper.py — Single-symbol analyzer subprocess.
Takes a symbol + exchange from stdin, logs in, fetches all contracts,
quotes for FUT + top options, returns detailed analysis JSON.
"""
import os, sys, json, io, contextlib, threading, re, gc

def _silent(fn, timeout=25):
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

def _f(v):
    try: return float(str(v).replace(",",""))
    except: return 0.0

def main():
    raw_in = sys.stdin.read().strip()
    if not raw_in:
        print(json.dumps({"error":"no_input"})); return
    params = json.loads(raw_in)
    symbol = params["symbol"].upper().strip()
    seg = params.get("exchange", "nse_fo")

    from neo_api_client import NeoAPI
    import pyotp

    ck   = os.environ["KOTAK_CONSUMER_KEY"]
    mob  = os.environ.get("KOTAK_MOBILE","").lstrip("+").lstrip("91")[-10:]
    ucc  = os.environ.get("KOTAK_UCC","")
    mpin = os.environ.get("KOTAK_MPIN","")
    totp = pyotp.TOTP(os.environ["KOTAK_TOTP_SECRET"]).now()

    api = _silent(lambda: NeoAPI(environment="prod", consumer_key=ck))
    ok = False
    for mfmt in [f"+91{mob}", mob, f"91{mob}"]:
        r1 = _silent(lambda m=mfmt: api.totp_login(mobile_number=m, ucc=ucc, totp=totp))
        if isinstance(r1, dict) and not r1.get("error"):
            ok = True; break
    if not ok:
        print(json.dumps({"error":"login_failed"})); return
    _silent(lambda: api.totp_validate(mpin=mpin))

    # Search scrip
    try:
        r = _silent(lambda: api.search_scrip(exchange_segment=seg, symbol=symbol))
        raw = r.get("data",[]) or r.get("result",[]) if isinstance(r,dict) else (r if isinstance(r,list) else [])
    except:
        raw = []

    if not raw:
        print(json.dumps({"error":f"No contracts found for {symbol} on {seg}","contracts":[]})); return

    # Collect FUTs + options
    def _sym(item): return str(item.get("pTrdSymbol",item.get("trdSym",item.get("symbol","")))).strip()
    def _tok(item):
        for k in ("pSymbol","token","instrument_token","pScripRefKey"):
            v=item.get(k)
            if v and str(v).strip(): return str(v).strip()
        return None
    def _matches(sym, target):
        s=sym.upper(); t=target.upper()
        if not s.startswith(t): return False
        rest=s[len(t):]
        return not rest or rest[0].isdigit()

    contracts = []
    for item in raw:
        s = _sym(item)
        if not _matches(s, symbol): continue
        tok = _tok(item)
        if not tok: continue

        ctype = "FUT" if "FUT" in s.upper() else None
        if not ctype:
            if s.upper().endswith("CE"): ctype = "CE"
            elif s.upper().endswith("PE"): ctype = "PE"
            else:
                raw_opt = str(item.get("pOptionType",item.get("optTp",""))).strip().upper()
                if raw_opt in ("CE","CALL","C"): ctype = "CE"
                elif raw_opt in ("PE","PUT","P"): ctype = "PE"
        if not ctype: continue

        sk = 0
        if ctype != "FUT":
            for k in ("pStrikePrice","strkPrc","strikePrice"):
                v = item.get(k)
                if v:
                    try: sk = float(v); break
                    except: pass

        contracts.append({"tok":tok,"seg":seg,"sym":s,"type":ctype,"strike":int(sk)})

    # Fetch quotes for FUTs + top 20 options by ATM proximity
    futs = [c for c in contracts if c["type"]=="FUT"][:3]
    opts = [c for c in contracts if c["type"] in ("CE","PE")]

    # Get underlying from FUT
    und = 0.0
    results = []
    for f in futs:
        try:
            qr = _silent(lambda f=f: api.quotes(
                instrument_tokens=[{"instrument_token":str(f["tok"]),"exchange_segment":f["seg"]}],
                quote_type=None))
            q = _extract_quote(qr)
            ltp = _f(q.get("ltp",0))
            vol = int(_f(q.get("last_volume",0)))
            oi = int(_f(q.get("open_int",0)))
            ltq = int(_f(q.get("last_traded_quantity",0)))
            tb = int(_f(q.get("total_buy",0)))
            ts_ = int(_f(q.get("total_sell",0)))
            chg = _f(q.get("change",0))
            pchg = _f(q.get("per_change",0))
            if ltp > 0 and und == 0: und = ltp
            side = "BUY-heavy" if tb > ts_*1.2 else "SELL-heavy" if ts_ > tb*1.2 else "balanced"
            results.append({
                "contract":f["sym"],"type":"FUT","strike":"-","ltp":ltp,
                "volume":vol,"oi":oi,"ltq":ltq,"change":chg,"pct_change":pchg,
                "buy_qty":tb,"sell_qty":ts_,"side":side,
            })
        except: pass

    # Sort options by ATM proximity, take top 20
    if und > 0:
        opts.sort(key=lambda o: abs(o["strike"] - und) if o["strike"] else 999999)
    opts = opts[:20]

    for o in opts:
        try:
            qr = _silent(lambda o=o: api.quotes(
                instrument_tokens=[{"instrument_token":str(o["tok"]),"exchange_segment":o["seg"]}],
                quote_type=None))
            q = _extract_quote(qr)
            ltp = _f(q.get("ltp",0))
            vol = int(_f(q.get("last_volume",0)))
            oi = int(_f(q.get("open_int",0)))
            ltq = int(_f(q.get("last_traded_quantity",0)))
            tb = int(_f(q.get("total_buy",0)))
            ts_ = int(_f(q.get("total_sell",0)))
            chg = _f(q.get("change",0))
            pchg = _f(q.get("per_change",0))
            side = "BUY-heavy" if tb > ts_*1.2 else "SELL-heavy" if ts_ > tb*1.2 else "balanced"
            results.append({
                "contract":o["sym"],"type":o["type"],"strike":o["strike"],
                "ltp":ltp,"volume":vol,"oi":oi,"ltq":ltq,
                "change":chg,"pct_change":pchg,
                "buy_qty":tb,"sell_qty":ts_,"side":side,
            })
        except: pass

    # Sort by volume (highest first)
    results.sort(key=lambda r: r["volume"], reverse=True)

    print(json.dumps({
        "symbol":symbol,"exchange":seg,"underlying":und,
        "total_contracts":len(contracts),"analyzed":len(results),
        "results":results,
    }))
    sys.stdout.flush()

if __name__=="__main__":
    main()
