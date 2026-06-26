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
    for k in ("pExpiryDate","pExpDate","expiry","expiryDate","expDate"):
        v=item.get(k)
        if not v: continue
        s=str(v).strip().upper()
        for fmt in ("%d%b%Y","%d-%b-%Y","%Y-%m-%d","%d%b%y","%d-%b-%y"):
            try:
                d=datetime.datetime.strptime(s,fmt).date()
                if d>=today: return str(d)
            except: pass
    s=_sym(item)
    m=re.search(r'(\d{1,2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})',s)
    if m:
        day,mon,yr=m.groups(); yi=2000+int(yr)
        import datetime as _dt, pytz, calendar
        mons=["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
        mo=mons.index(mon)+1; today=_dt.datetime.now(pytz.timezone("Asia/Kolkata")).date()
        if int(day) in range(1,29):
            # MCX style: day embedded in symbol
            try:
                d=_dt.date(yi,mo,int(day))
                if d>=today: return str(d)
            except: pass
        # NSE style: last thursday of month
        last_day=calendar.monthrange(yi,mo)[1]
        d=_dt.date(yi,mo,last_day)
        while d.weekday()!=3: d-=_dt.timedelta(days=1)
        if d>=today: return str(d)
    return None

SYMBOLS = {
    "nse_fo": ["NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY","RELIANCE","HDFCBANK","TCS","INFY","ICICIBANK","SBIN"],
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
    mob=os.environ.get("KOTAK_MOBILE","").strip().replace(" ","").replace("-","")
    if mob.startswith("+91"): mob=mob[3:]
    elif mob.startswith("91") and len(mob)==12: mob=mob[2:]
    elif mob.startswith("0") and len(mob)==11: mob=mob[1:]
    padded=secret+"="*(-len(secret)%8)
    try: totp=pyotp.TOTP(padded).now()
    except: totp=pyotp.TOTP(secret).now()

    nfk=os.environ.get("KOTAK_NEO_FIN_KEY","").strip()
    api=_silent(lambda: NeoAPI(environment="prod",consumer_key=ck,neo_fin_key=nfk) if nfk else NeoAPI(environment="prod",consumer_key=ck))
    logged_in=False
    for mfmt in [f"+91{mob}"]:
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

    # ══ SCAN: collect FUTs + options, volume-rank, keep top per category ═════
    import datetime as _dt, pytz as _pytz, time as _time
    _now=_dt.datetime.now(_pytz.timezone("Asia/Kolkata")); _wd=_now.weekday()
    _nse=(_now.replace(hour=9,minute=15,second=0)<=_now<=_now.replace(hour=15,minute=30,second=0)) and _wd<5
    _mcx=((_wd<5) and _now.replace(hour=9,minute=0,second=0)<=_now<=_now.replace(hour=23,minute=30,second=0)) or \
         ((_wd==5) and _now.replace(hour=9,minute=0,second=0)<=_now<=_now.replace(hour=14,minute=0,second=0))

    TOP_N = 20
    OPTS_PER_SYM = 4

    def _vol_of(q):
        for k in ("last_volume","volume","vol"):
            v=q.get(k)
            if v not in (None,""):
                try: return int(_f(v))
                except: pass
        return 0

    # Category mapping matching app.py
    STOCK_SET={"RELIANCE","HDFCBANK","TCS","INFY","ICICIBANK","SBIN"}
    INDEX_SET={"NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY"}
    def _cat(s): return "Stock" if s in STOCK_SET else "Index" if s in INDEX_SET else "Commodity"

    candidates={"Index":[], "Stock":[], "Commodity":[]}
    all_fut_quotes={}  # accumulate FUT quotes for output

    for seg,symbols in SYMBOLS.items():
        for symbol in symbols:
            cat=_cat(symbol)   # INSIDE the loop (was outside = bug)
            try:
                r=_silent(lambda s=seg,sym=symbol: api.search_scrip(exchange_segment=s,symbol=sym))
                raw=r.get("data",[]) or r.get("result",[]) if isinstance(r,dict) else (r if isinstance(r,list) else [])
            except Exception as ex:
                raw=[]
                print(f"[scrip] {symbol}/{seg} search err: {ex}", file=sys.stderr, flush=True)
            print(f"[scrip] {symbol}/{seg}: {len(raw)} records", file=sys.stderr, flush=True)

            # Dump ONE sample non-FUT record so we can see the actual field names
            for item in raw:
                s=_sym(item)
                if _matches(s,symbol) and "FUT" not in s.upper():
                    print(f"[sample] {symbol} option: {json.dumps({k:str(v)[:40] for k,v in item.items()},default=str)}", file=sys.stderr, flush=True)
                    break

            # ── Collect FUTs (unconditionally — only 2-3 exist per symbol) ────
            futs=[]
            for item in raw:
                s=_sym(item)
                if not _matches(s,symbol): continue
                if "FUT" not in s.upper(): continue
                tok=_tok(item)
                if not tok: continue
                ep=_parse_exp(item)
                futs.append({"tok":tok,"seg":seg,"sym":s,"type":"FUT","strike":None,"expiry":ep or "?","symbol":symbol})
            futs.sort(key=lambda x: x["expiry"])
            for f in futs[:2]: candidates[cat].append(f)

            # Get underlying price from nearest FUT (and save quote for later)
            und=0.0
            if futs:
                try:
                    qr=_silent(lambda f=futs[0]: api.quotes(
                        instrument_tokens=[{"instrument_token":str(futs[0]["tok"]),"exchange_segment":futs[0]["seg"]}],
                        quote_type=None))
                    q=_extract_quote(qr)
                    if q: all_fut_quotes[str(futs[0]["tok"])]=q  # save for output
                    for k in ("ltp","last_price","close"):
                        v=q.get(k)
                        if v not in (None,"",0,"0","0.0000"):
                            try:
                                fv=float(str(v).replace(",",""))
                                if fv>0: und=fv; break
                            except: pass
                except: pass
            print(f"[scan] {symbol}: {len(futs)} FUTs, und=₹{und}", file=sys.stderr, flush=True)

            # ── Collect options — parse from SYMBOL STRING (most reliable) ────
            # Don't rely on separate fields — parse CE/PE and strike from symbol
            all_opts=[]
            for item in raw:
                s=_sym(item).upper()
                if not _matches(s,symbol): continue
                if "FUT" in s: continue
                tok=_tok(item)
                if not tok: continue
                # Parse option type: check symbol ending first, then field
                opt=None
                if s.endswith("CE"): opt="CE"
                elif s.endswith("PE"): opt="PE"
                if not opt:
                    raw_opt=str(item.get("pOptionType",item.get("optTp",""))).strip().upper()
                    opt="CE" if raw_opt in("CE","CALL","C") else "PE" if raw_opt in("PE","PUT","P") else None
                if not opt: continue
                # Parse strike: from field first, fallback to symbol regex
                sk=None
                for k in ("pStrikePrice","strkPrc","strikePrice"):
                    v=item.get(k)
                    if v is not None:
                        try: sk=float(v)
                        except: pass
                        break
                if not sk or sk<=0:
                    m=re.search(r"(\d+)(CE|PE)$", s)
                    if m:
                        try: sk=float(m.group(1))
                        except: pass
                if not sk or sk>0:
                    if sk is None: sk=0
                    ep=_parse_exp(item)
                    all_opts.append({"tok":tok,"seg":seg,"sym":_sym(item),"type":opt,
                                     "strike":int(sk) if sk else 0,"expiry":ep or "?",
                                     "symbol":symbol,"_sk":sk or 0})

            print(f"[scan] {symbol}: {len(all_opts)} options parsed", file=sys.stderr, flush=True)

            if all_opts:
                # Keep only nearest 2 option expiries
                opt_exps=sorted(set(o["expiry"] for o in all_opts if o["expiry"]!="?"))
                if opt_exps:
                    keep=set(opt_exps[:2])
                    all_opts=[o for o in all_opts if o["expiry"] in keep or o["expiry"]=="?"]
                    print(f"[scan] {symbol}: kept expiries {keep}, {len(all_opts)} after filter", file=sys.stderr, flush=True)
                # Near ATM: keep strikes closest to underlying
                if und>0 and len(all_opts)>OPTS_PER_SYM:
                    all_opts.sort(key=lambda o: abs(o["_sk"]-und) if o["_sk"] else 999999)
                    all_opts=all_opts[:OPTS_PER_SYM]
                for o in all_opts:
                    o.pop("_sk",None)
                    candidates[cat].append(o)

            del raw; gc.collect()

    # ── Collect tokens — NO option quote fetching (quote_helper does that live)
    quotes=dict(all_fut_quotes); token_map={}

    for cat,conts in candidates.items():
        futs=[c for c in conts if c["type"]=="FUT"]
        opts=[c for c in conts if c["type"] in ("CE","PE")]
        token_map[cat]=futs+opts
        nf=len(futs); no=len(opts)
        print(f"[result] {cat}: {nf} FUT + {no} options = {nf+no} tokens", file=sys.stderr, flush=True)

    # ── Diagnostic ────────────────────────────────────────────────────────────
    diag={}
    _first=None
    for cat,entries in token_map.items():
        if entries: _first=entries[0]; break
    if _first:
        q=quotes.get(str(_first["tok"]),{})
        diag["quote_keys"]=str(list(q.keys()))
        diag["sample"]=f"{_first['sym']} ltp={q.get('ltp')}"

    total=sum(len(v) for v in token_map.values())
    print(f"[done] total {total} tokens, {len(quotes)} quotes", file=sys.stderr, flush=True)

    result={"session":session,"token_map":token_map,"quotes":quotes,"diag":diag,"ck":ck}
    print(json.dumps(result))
    sys.stdout.flush()

if __name__=="__main__":
    main()
