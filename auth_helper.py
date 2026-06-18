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
    "nse_fo": ["NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY",
               "RELIANCE","HDFCBANK","TCS","INFY","ICICIBANK","SBIN"],
    "mcx_fo": ["GOLDM","SILVERM","CRUDEOIL","NATURALGAS","COPPER"],
}
STEPS = {
    "NIFTY":50,"BANKNIFTY":100,"FINNIFTY":50,"MIDCPNIFTY":25,
    "RELIANCE":50,"HDFCBANK":20,"TCS":100,"INFY":50,"ICICIBANK":20,"SBIN":10,
    "GOLDM":100,"SILVERM":1000,"CRUDEOIL":100,"NATURALGAS":10,"COPPER":5,
}
STRIKE_RANGE=2

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

    # Collect minimal tokens: nearest FUT + ATM±STRIKE_RANGE options per symbol
    ist=pytz.timezone("Asia/Kolkata")
    today=datetime.datetime.now(ist).date()
    token_map={}   # symbol → [{token,seg,sym,type,strike,expiry}]

    # Only scan the segment whose market is open right now (saves time)
    import datetime as _dt, pytz as _pytz
    _now=_dt.datetime.now(_pytz.timezone("Asia/Kolkata")); _wd=_now.weekday()
    _nse=(_now.replace(hour=9,minute=15,second=0)<=_now<=_now.replace(hour=15,minute=30,second=0)) and _wd<5
    _mcx=((_wd<5) and _now.replace(hour=9,minute=0,second=0)<=_now<=_now.replace(hour=23,minute=30,second=0)) or \
         ((_wd==5) and _now.replace(hour=9,minute=0,second=0)<=_now<=_now.replace(hour=14,minute=0,second=0))

    for seg,symbols in SYMBOLS.items():
        # Skip closed segments to avoid slow hanging quote calls
        if seg in ("nse_fo","nse_cm") and not _nse: continue
        if seg=="mcx_fo" and not _mcx: continue
        for symbol in symbols:
            step=STEPS.get(symbol,50)
            try:
                r=_silent(lambda s=seg,sym=symbol: api.search_scrip(exchange_segment=s,symbol=sym))
                raw=r.get("data",[]) or r.get("result",[]) if isinstance(r,dict) else (r if isinstance(r,list) else [])
            except: raw=[]

            entries=[]
            # Nearest FUT — ONE live quote to get underlying for ATM calc
            futs=sorted([
                (ep,item) for item in raw
                for ep in [_parse_exp(item)]
                if ep and "FUT" in _sym(item) and _matches(_sym(item),symbol)
            ], key=lambda x:x[0])

            und=0.0
            if futs:
                ep,item=futs[0]; tok=_tok(item)
                entries.append({"tok":tok,"seg":seg,"sym":_sym(item),
                                 "type":"FUT","strike":None,"expiry":ep})
                try:
                    qr=_silent(lambda t=tok,s=seg: api.quotes(
                        instrument_tokens=[{"instrument_token":str(t),"exchange_segment":s}],
                        quote_type="ltp"))
                    q=_extract_quote(qr)
                    for k in ("ltp","last_price","last_traded_price","close"):
                        v=q.get(k)
                        if v not in (None,"",0,"0",0.0,"0.0000"):
                            f=_f(v)
                            if f>0: und=f; break
                except Exception as ex:
                    print(f"[und] {symbol} err: {ex}", file=sys.stderr, flush=True)

            # Options near ATM — collect tokens only, NO live quotes (fast)
            if und>0:
                atm=round(und/step)*step
                watch={atm+i*step for i in range(-STRIKE_RANGE,STRIKE_RANGE+1)}
                for item in raw:
                    s2=_sym(item)
                    if not _matches(s2,symbol): continue
                    raw_opt=str(item.get("pOptionType",item.get("optTp",""))).strip().upper()
                    opt="CE" if raw_opt in("CE","CALL","C") else "PE" if raw_opt in("PE","PUT","P") else None
                    if not opt: continue
                    for k in ("pStrikePrice","strkPrc","strikePrice"):
                        v=item.get(k)
                        if v is not None:
                            try:
                                sk=float(v)
                                if sk in watch:
                                    ep=_parse_exp(item)
                                    if ep:
                                        entries.append({"tok":_tok(item),"seg":seg,"sym":s2,
                                                        "type":opt,"strike":int(sk),"expiry":ep})
                            except: pass
                            break

            token_map[symbol]=entries
            del raw; gc.collect()
            print(f"[auth] {symbol}: {len(entries)} tokens", file=sys.stderr, flush=True)

    # ── DIAGNOSTIC: capture exactly what the quote API returns ────────────────
    diag={}
    try:
        diag["methods"]=[m for m in dir(api) if not m.startswith("_") and
                         any(x in m.lower() for x in ("quote","ltp","market","depth","subscribe","feed"))]
    except Exception as ex: diag["methods_err"]=str(ex)

    # Try get_live_quotes on the first available token, capture raw + error
    _first=None
    for sym,entries in token_map.items():
        for e in entries:
            if e.get("tok"): _first=(e["tok"],e["seg"]); break
        if _first: break

    if _first:
        tk,sg=_first
        diag["test_token"]=f"{tk}/{sg}"
        # Show FULL quote so we can see every field name (esp. the LTP field)
        try:
            r=_silent(lambda: api.quotes(
                instrument_tokens=[{"instrument_token":str(tk),"exchange_segment":sg}],
                quote_type=None))
            q=_extract_quote(r)
            diag["quote_keys"]=str(list(q.keys()))
            diag["quote_full"]=str(q)[:800]
        except Exception as ex:
            diag["quote_err"]=str(ex)[:300]

    # ── Fetch LIVE QUOTES for all tokens via SDK ──────────────────────────────
    quotes={}
    all_tokens=[]
    # Prioritize FUT first, then options — cap at 60 to stay within timeout
    for sym,entries in token_map.items():
        for e in entries:
            if e.get("tok") and e.get("type")=="FUT":
                all_tokens.append((e["tok"], e["seg"]))
    for sym,entries in token_map.items():
        for e in entries:
            if e.get("tok") and e.get("type")!="FUT":
                all_tokens.append((e["tok"], e["seg"]))
    all_tokens=all_tokens[:60]

    # Group by segment, batch fetch
    by_seg={}
    for tok,seg in all_tokens:
        by_seg.setdefault(seg,[]).append(str(tok))

    for seg,toks in by_seg.items():
        for tk in toks:
            try:
                qr=_silent(lambda tk=tk,seg=seg: api.quotes(
                    instrument_tokens=[{"instrument_token":str(tk),"exchange_segment":seg}],
                    quote_type=None))   # None = full quote (ltp+vol+oi+depth)
                q=_extract_quote(qr)
                if q:
                    quotes[tk]=q
            except Exception as ex:
                print(f"[quote] {tk} err: {ex}", file=sys.stderr, flush=True)

    print(f"[quote] fetched {len(quotes)} quotes for {len(all_tokens)} tokens", file=sys.stderr, flush=True)

    # Output: session + token map + LIVE QUOTES
    result={"session":session,"token_map":token_map,"quotes":quotes,"diag":diag,"ck":ck}
    print(json.dumps(result))
    sys.stdout.flush()

if __name__=="__main__":
    main()
