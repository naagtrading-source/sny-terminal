"""
auth_helper.py — runs as a SHORT-LIVED subprocess.
Loads the heavy SDK, logs in, extracts tokens for our symbols,
prints a compact JSON to stdout, then EXITS (freeing all SDK memory).
"""
import os, sys, json, re, io, contextlib, threading, pyotp, gc
gc.enable()

def _silent(fn):
    r=[None]; e=[None]
    def w():
        buf=io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                r[0]=fn()
        except Exception as ex: e[0]=ex
    t=threading.Thread(target=w,daemon=True); t.start(); t.join(timeout=45)
    if e[0]: raise e[0]
    return r[0]

def _f(v):
    try: return float(str(v).replace(",",""))
    except: return 0.0

def _extract_quote(qr):
    """Normalize whatever api.quotes() returns into a flat dict."""
    if qr is None: return {}
    if isinstance(qr,str): return {}
    if isinstance(qr,list):
        return qr[0] if qr and isinstance(qr[0],dict) else {}
    if isinstance(qr,dict):
        # Common wrappers: {"data":[...]}, {"data":{...}}, {"d":[...]}
        for dk in ("data","Data","result","Result","d","quotes"):
            if dk in qr:
                inn=qr[dk]
                if isinstance(inn,list) and inn:
                    return inn[0] if isinstance(inn[0],dict) else {}
                if isinstance(inn,dict): return inn
        return qr
    return {}

def _sym(item):
    for k in ("pTrdSymbol","trdSym","tradingSymbol"):
        v=item.get(k)
        if v: return str(v).upper().strip()
    return ""

def _tok(item):
    for k in ("pSymbol","token","instrument_token","Token"):
        v=item.get(k)
        if v is not None: return str(v)
    return None

def _matches(trd, target):
    trd=trd.upper(); target=target.upper()
    if not trd.startswith(target): return False
    rest=trd[len(target):]
    return (not rest) or rest[0].isdigit() or rest[0] in ("-"," ")

def _parse_exp(item):
    import datetime, pytz
    today=datetime.datetime.now(pytz.timezone("Asia/Kolkata")).date()
    for k in ("pExpDate","expiry","expiryDate","expDate"):
        v=item.get(k)
        if not v: continue
        s=str(v).strip().upper()
        for fmt in ("%d%b%Y","%d-%b-%Y","%Y-%m-%d","%d%b%y","%d-%b-%y"):
            try:
                d=datetime.datetime.strptime(s,fmt).date()
                if d>=today: return str(d)
            except: pass
    s=_sym(item)
    m=re.search(r'(\d{1,2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2,4})',s)
    if m:
        day,mon,yr=m.groups(); yi=int(yr) if len(yr)==4 else 2000+int(yr)
        import datetime
        try:
            d=datetime.datetime.strptime(f"{int(day):02d}{mon}{yi}","%d%b%Y").date()
            import pytz
            if d>=datetime.datetime.now(pytz.timezone("Asia/Kolkata")).date(): return str(d)
        except: pass
    return None

SYMBOLS = {
    "nse_fo": ["NIFTY","BANKNIFTY"],
    "mcx_fo": ["GOLDM","SILVERM","CRUDEOIL","NATURALGAS","COPPER"],
}
STEPS = {
    "NIFTY":50,"BANKNIFTY":100,"FINNIFTY":50,"MIDCPNIFTY":25,
    "RELIANCE":50,"HDFCBANK":20,"TCS":100,"INFY":50,"ICICIBANK":20,"SBIN":10,
    "GOLDM":100,"SILVERM":1000,"CRUDEOIL":100,"NATURALGAS":10,"COPPER":5,
}
STRIKE_RANGE=1

def main():
    from neo_api_client import NeoAPI
    import datetime, pytz

    ck=os.environ.get("KOTAK_CONSUMER_KEY","").strip()
    secret=os.environ.get("KOTAK_TOTP_SECRET","").replace(" ","")
    ucc=os.environ.get("KOTAK_UCC","").strip()
    mpin=os.environ.get("KOTAK_MPIN","").strip()
    mob=os.environ.get("KOTAK_MOBILE","").strip().lstrip("+").replace(" ","").replace("-","")
    if mob.startswith("91") and len(mob)==12: mob=mob[2:]
    elif mob.startswith("0") and len(mob)==11: mob=mob[1:]
    padded=secret+"="*(-len(secret)%8)
    try: totp=pyotp.TOTP(padded).now()
    except: totp=pyotp.TOTP(secret).now()

    api=_silent(lambda: NeoAPI(environment="prod",consumer_key=ck))
    logged_in=False
    for mfmt in [f"+91{mob}",mob,f"91{mob}"]:
        r1=_silent(lambda m=mfmt: api.totp_login(mobile_number=m,ucc=ucc,totp=totp))
        if isinstance(r1,dict) and not r1.get("error"):
            logged_in=True; break
    if not logged_in:
        print(json.dumps({"error":"login_failed"})); sys.exit(1)
    _silent(lambda: api.totp_validate(mpin=mpin))

    # (session-save removed — was causing hangs)

    # Extract session headers — capture EVERYTHING that could be auth-related
    session = {}
    def _grab(obj, prefix=""):
        for attr in dir(obj):
            if attr.startswith("__"): continue
            try:
                v=getattr(obj,attr)
                if callable(v): continue
                sv=str(v)
                if 3<len(sv)<800 and any(x in attr.lower() for x in
                    ("auth","token","sid","access","key","session","bearer","header","hsserverid","serverid")):
                    session[f"{prefix}{attr}"]=sv
            except: pass
    _grab(api)
    for sub in ("configuration","session","api_client"):
        try: _grab(getattr(api,sub), f"{sub}_")
        except: pass

    # CRITICAL: capture the SDK's actual request headers by inspecting
    # the api_client.default_headers or similar
    try:
        ac=api.api_client
        if hasattr(ac,"default_headers"):
            for hk,hv in dict(ac.default_headers).items():
                session[f"hdr_{hk}"]=str(hv)
    except: pass
    try:
        if hasattr(api,"configuration"):
            cfg=api.configuration
            if hasattr(cfg,"api_key") and isinstance(cfg.api_key,dict):
                for kk,kv in cfg.api_key.items():
                    session[f"apikey_{kk}"]=str(kv)
    except: pass

    # ══ VOLUME-FIRST SCAN — FUTs first, then options near ATM ══════════════════
    # ROOT CAUSE OF OLD BUG: FUTs and options share one expiry pool.
    # MCX options have WEEKLY expiries, FUTs have MONTHLY. Mixing them in
    # keep_exps[:2] picks 2 weekly dates, dropping everything else.
    # FIX: collect FUTs unconditionally (there are few), then options separately
    # with their OWN nearest expiry, capped to prevent API call explosion.

    import datetime as _dt, pytz as _pytz
    _now=_dt.datetime.now(_pytz.timezone("Asia/Kolkata")); _wd=_now.weekday()
    _nse=(_now.replace(hour=9,minute=15,second=0)<=_now<=_now.replace(hour=15,minute=30,second=0)) and _wd<5
    _mcx=((_wd<5) and _now.replace(hour=9,minute=0,second=0)<=_now<=_now.replace(hour=23,minute=30,second=0)) or \
         ((_wd==5) and _now.replace(hour=9,minute=0,second=0)<=_now<=_now.replace(hour=14,minute=0,second=0))

    TOP_N = 20          # keep this many highest-volume contracts per category
    OPTS_PER_SYM = 12   # max option candidates per symbol before volume-ranking

    def _vol_of(q):
        for k in ("last_volume","volume","vol","volume_traded"):
            v=q.get(k)
            if v not in (None,""):
                try: return int(_f(v))
                except: pass
        return 0

    candidates={"Index":[], "Commodity":[]}
    cat_of={"nse_fo":"Index", "mcx_fo":"Commodity"}

    for seg,symbols in SYMBOLS.items():
        if seg in ("nse_fo","nse_cm") and not _nse: continue
        if seg=="mcx_fo" and not _mcx: continue
        cat=cat_of.get(seg,"Index")
        for symbol in symbols:
            try:
                r=_silent(lambda s=seg,sym=symbol: api.search_scrip(exchange_segment=s,symbol=sym))
                raw=r.get("data",[]) or r.get("result",[]) if isinstance(r,dict) else (r if isinstance(r,list) else [])
            except Exception as ex:
                raw=[]
                print(f"[scrip] {symbol}/{seg} err: {ex}", file=sys.stderr, flush=True)
            print(f"[scrip] {symbol}/{seg}: {len(raw)} records", file=sys.stderr, flush=True)

            # ── PASS 1: Collect ALL FUTs (no expiry filter — only ~2-3 exist) ──
            futs_found = []
            for item in raw:
                s=_sym(item)
                if not _matches(s, symbol): continue
                if "FUT" not in s: continue
                ep=_parse_exp(item); tok=_tok(item)
                if not tok: continue
                futs_found.append({"tok":tok,"seg":seg,"sym":s,"type":"FUT",
                                   "strike":None,"expiry":ep or "?","symbol":symbol})
            # Keep nearest 2 FUTs by expiry
            futs_found.sort(key=lambda x: x["expiry"])
            for f in futs_found[:2]:
                candidates[cat].append(f)

            # Get FUT underlying price for ATM strike selection
            und = 0.0
            if futs_found:
                try:
                    qr=_silent(lambda f=futs_found[0]: api.quotes(
                        instrument_tokens=[{"instrument_token":str(f["tok"]),"exchange_segment":f["seg"]}],
                        quote_type=None))
                    q=_extract_quote(qr)
                    for k in ("ltp","last_price","close"):
                        v=q.get(k)
                        if v not in (None,"",0,"0","0.0000"):
                            try:
                                fv=float(str(v).replace(",",""))
                                if fv>0: und=fv; break
                            except: pass
                except: pass
            print(f"[scan] {symbol}: {len(futs_found)} FUTs, underlying=₹{und}", file=sys.stderr, flush=True)

            # ── PASS 2: Collect options — own expiry tracking, near ATM ────────
            # First: gather all option records with their parsed data
            all_opts = []
            n_matched=0; n_exp=0; n_type=0; n_strike=0
            for item in raw:
                s=_sym(item)
                if not _matches(s, symbol): continue
                if "FUT" in s: continue
                n_matched+=1
                ep=_parse_exp(item)
                if not ep:
                    n_exp+=1; continue
                # Detect option type from pOptionType OR from the symbol suffix
                raw_opt=str(item.get("pOptionType",item.get("optTp",""))).strip().upper()
                opt="CE" if raw_opt in("CE","CALL","C") else "PE" if raw_opt in("PE","PUT","P") else None
                if not opt:
                    # Try parsing from symbol: ends with CE or PE
                    if s.upper().endswith("CE"): opt="CE"
                    elif s.upper().endswith("PE"): opt="PE"
                if not opt:
                    n_type+=1; continue
                sk=None
                for k in ("pStrikePrice","strkPrc","strikePrice"):
                    v=item.get(k)
                    if v is not None:
                        try: sk=float(v)
                        except: pass
                        break
                if not sk or sk<=0:
                    # Try extracting strike from symbol (digits before CE/PE)
                    # re already imported at module level
                    m=re.search(r'(\d+)(CE|PE)$', s.upper())
                    if m:
                        try: sk=float(m.group(1))
                        except: pass
                if not sk or sk<=0:
                    n_strike+=1; continue
                tok=_tok(item)
                if not tok: continue
                all_opts.append({"tok":tok,"seg":seg,"sym":s,"type":opt,
                                 "strike":int(sk),"expiry":ep,"symbol":symbol,"_sk":sk})

            print(f"[scan] {symbol}: {n_matched} non-FUT matched, {len(all_opts)} options parsed "
                  f"(dropped: {n_exp} no-exp, {n_type} no-type, {n_strike} no-strike)",
                  file=sys.stderr, flush=True)

            if all_opts:
                # Option-specific nearest expiry (separate from FUT expiries!)
                opt_exps = sorted(set(o["expiry"] for o in all_opts))
                keep_opt_exps = set(opt_exps[:2])  # nearest 2 OPTION expiries
                all_opts = [o for o in all_opts if o["expiry"] in keep_opt_exps]
                print(f"[scan] {symbol}: option expiries kept={keep_opt_exps}, {len(all_opts)} after exp filter",
                      file=sys.stderr, flush=True)

                # Near ATM: if we have underlying, keep strikes closest to it
                if und > 0 and len(all_opts) > OPTS_PER_SYM:
                    all_opts.sort(key=lambda o: abs(o["_sk"] - und))
                    all_opts = all_opts[:OPTS_PER_SYM]

                for o in all_opts:
                    del o["_sk"]
                    candidates[cat].append(o)

            del raw; gc.collect()

    # ── Batch-fetch volume for ALL candidates, then rank ──────────────────────
    quotes={}
    token_map={}   # category → top contracts (with quotes attached via quotes dict)

    import time as _time
    scan_start=_time.time()
    SCAN_BUDGET=120   # seconds max for the whole volume-ranking pass

    for cat,conts in candidates.items():
        print(f"[scan] {cat}: {len(conts)} candidate contracts", file=sys.stderr, flush=True)
        vols=[]   # (volume, contract, quote)
        for c in conts:
            if _time.time()-scan_start > SCAN_BUDGET:
                print(f"[scan] {cat}: time budget hit, ranking {len(vols)} so far", file=sys.stderr, flush=True)
                break
            try:
                qr=_silent(lambda c=c: api.quotes(
                    instrument_tokens=[{"instrument_token":str(c["tok"]),"exchange_segment":c["seg"]}],
                    quote_type=None))
                q=_extract_quote(qr)
                v=_vol_of(q)
                if v>0:
                    vols.append((v,c,q))
            except Exception as ex:
                pass
        # Rank by volume — keep top FUTs AND top options separately
        # (so high-volume FUTs don't crowd out the option strikes you want)
        vols.sort(key=lambda x:x[0], reverse=True)
        futs = [(v,c,q) for v,c,q in vols if c["type"]=="FUT"][:6]
        opts = [(v,c,q) for v,c,q in vols if c["type"] in ("CE","PE")][:TOP_N]
        top = futs + opts
        entries=[]
        for v,c,q in top:
            quotes[str(c["tok"])]=q
            entries.append(c)
        token_map[cat]=entries
        n_fut=sum(1 for e in entries if e["type"]=="FUT")
        n_opt=len(entries)-n_fut
        print(f"[scan] {cat}: kept {n_fut} FUT + {n_opt} options by volume",
              file=sys.stderr, flush=True)

    # ── Diagnostic: capture one full quote for field reference ────────────────
    diag={}
    _first=None
    for cat,entries in token_map.items():
        if entries: _first=entries[0]; break
    if _first:
        q=quotes.get(str(_first["tok"]),{})
        diag["quote_keys"]=str(list(q.keys()))
        diag["sample"]=f"{_first['sym']} vol={_vol_of(q)} ltp={q.get('ltp')}"

    result={"session":session,"token_map":token_map,"quotes":quotes,"diag":diag,"ck":ck}
    print(json.dumps(result))
    sys.stdout.flush()

if __name__=="__main__":
    main()
