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
STRIKE_RANGE=3

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

    # Extract session headers from api object
    session = {}
    for attr in dir(api):
        if any(x in attr.lower() for x in ("auth","token","sid","access")):
            try:
                v=getattr(api,attr)
                if v and not callable(v) and 5<len(str(v))<500:
                    session[attr]=str(v)
            except: pass
    try:
        cfg=api.configuration
        for attr in ("auth","token","access_token","sid","server_id","Authorization"):
            try:
                v=getattr(cfg,attr,None)
                if v and 5<len(str(v))<500: session[f"cfg_{attr}"]=str(v)
            except: pass
    except: pass

    # Collect minimal tokens: nearest FUT + ATM±STRIKE_RANGE options per symbol
    ist=pytz.timezone("Asia/Kolkata")
    today=datetime.datetime.now(ist).date()
    token_map={}   # symbol → [{token,seg,sym,type,strike,expiry}]

    for seg,symbols in SYMBOLS.items():
        for symbol in symbols:
            step=STEPS.get(symbol,50)
            try:
                r=_silent(lambda s=seg,sym=symbol: api.search_scrip(exchange_segment=s,symbol=sym))
                raw=r.get("data",[]) or r.get("result",[]) if isinstance(r,dict) else (r if isinstance(r,list) else [])
            except: raw=[]

            entries=[]
            # Nearest FUT
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
                # Get underlying from futures LTP (we'll do this with raw HTTP later)
                # Use strike midpoint as approximate ATM for now
                # Actually try to get LTP from the quote
                try:
                    qr=_silent(lambda t=tok,s=seg: api.get_live_quotes(
                        [{"instrument_token":str(t),"exchange_segment":s}]))
                    if isinstance(qr,list) and qr:
                        q=qr[0]
                    elif isinstance(qr,dict) and "data" in qr:
                        d=qr["data"]; q=d[0] if isinstance(d,list) and d else (d if isinstance(d,dict) else {})
                    else: q={}
                    for k in ("ltp","last_traded_price","lastPrice","LTP","c","close","price"):
                        v=q.get(k)
                        if v not in (None,"",0,"0",0.0):
                            f=_f(v)
                            if f>0: und=f; break
                except: pass

            # Options near ATM
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

    # Output: session headers + token map
    result={"session":session,"token_map":token_map,"ck":ck}
    print(json.dumps(result))
    sys.stdout.flush()

if __name__=="__main__":
    main()
