"""
auth_helper.py — Dhan-based token discovery.
Downloads instrument master, finds FUT + ATM options for our symbols,
returns token_map + quotes JSON. No TOTP, no session login.
"""
import os, sys, json, re, gc, io, csv, urllib.request, datetime
import pytz

def _f(v):
    try: return float(str(v).replace(",",""))
    except: return 0.0

SYMBOLS = {
    "nse_fo": ["NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY","RELIANCE","HDFCBANK","TCS","INFY","ICICIBANK","SBIN","AXISBANK","KOTAKBANK","LT","WIPRO","BAJFINANCE","TITAN","MARUTI","SUNPHARMA","TATAMOTORS","ADANIENT"],
    "mcx_fo": ["GOLDM","SILVERM","CRUDEOIL","NATURALGAS","COPPER"],
}
STOCK_SET  = {"RELIANCE","HDFCBANK","TCS","INFY","ICICIBANK","SBIN","AXISBANK","KOTAKBANK","LT","WIPRO","BAJFINANCE","TITAN","MARUTI","SUNPHARMA","TATAMOTORS","ADANIENT"}
INDEX_SET  = {"NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY"}
STEPS = {
    "NIFTY":50,"BANKNIFTY":100,"FINNIFTY":50,"MIDCPNIFTY":25,
    "RELIANCE":50,"HDFCBANK":20,"TCS":100,"INFY":50,"ICICIBANK":20,"SBIN":10,
    "AXISBANK":20,"KOTAKBANK":20,"LT":50,"WIPRO":10,"BAJFINANCE":50,
    "TITAN":50,"MARUTI":100,"SUNPHARMA":20,"TATAMOTORS":10,"ADANIENT":50,
    "GOLDM":100,"SILVERM":1000,"CRUDEOIL":100,"NATURALGAS":10,"COPPER":5,
}
OPTS_PER_SYM = 4
SEG_MAP = {"nse_fo": "NSE_FNO", "mcx_fo": "MCX_COMM"}

def _cat(s):
    return "Stock" if s in STOCK_SET else "Index" if s in INDEX_SET else "Commodity"

def main():
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    except Exception: pass
    client_id    = os.environ["DHAN_CLIENT_ID"].strip()
    access_token = os.environ["DHAN_ACCESS_TOKEN"].strip()

    from dhanhq import DhanContext, dhanhq
    ctx  = DhanContext(client_id, access_token)
    dhan = dhanhq(ctx)

    url = "https://images.dhan.co/api-data/api-scrip-master.csv"
    with urllib.request.urlopen(url, timeout=30) as resp:
        lines = resp.read().decode("utf-8").splitlines()
    reader = csv.DictReader(lines)
    master = list(reader)
    print(f"[master] {len(master)} rows", file=sys.stderr)

    today = datetime.datetime.now(pytz.timezone("Asia/Kolkata")).date()
    token_map  = {"Index": [], "Stock": [], "Commodity": []}
    candidates = {"Index": [], "Stock": [], "Commodity": []}

    for seg, symbols in SYMBOLS.items():
        dhan_seg = SEG_MAP[seg]
        exch = "NSE" if seg == "nse_fo" else "MCX"
        for symbol in symbols:
            cat = _cat(symbol)
            sym_rows = []
            for row in master:
                if row.get("SEM_EXM_EXCH_ID","") != exch: continue
                tsym = row.get("SEM_TRADING_SYMBOL","")
                if not (tsym.upper().startswith(symbol+"-") or tsym.upper().startswith(symbol+" ")): continue
                exp_str = row.get("SEM_EXPIRY_DATE","")
                try:
                    exp_d = datetime.datetime.strptime(exp_str.strip()[:10], "%Y-%m-%d").date()
                    if exp_d < today: continue
                except: pass
                sym_rows.append(row)

            print(f"[scan] {symbol}/{seg}: {len(sym_rows)} active contracts", file=sys.stderr)

            futs = [r for r in sym_rows if "FUT" in r.get("SEM_INSTRUMENT_NAME","").upper()]
            futs.sort(key=lambda r: r.get("SEM_EXPIRY_DATE",""))
            for r in futs[:2]:
                sec_id = r["SEM_SMST_SECURITY_ID"]
                tsym   = r["SEM_TRADING_SYMBOL"]
                exp    = r.get("SEM_EXPIRY_DATE","?")[:10]
                candidates[cat].append({
                    "tok": sec_id, "seg": dhan_seg, "sym": tsym,
                    "type": "FUT", "strike": None, "expiry": exp, "symbol": symbol
                })

            # FIX: try multiple futs for underlying price (futs[0] may be illiquid on expiry day)
            und = 0.0
            for fr in futs[:3]:
                try:
                    sec_id = fr["SEM_SMST_SECURITY_ID"]
                    r2 = dhan.ohlc_data(securities={dhan_seg: [int(sec_id)]})
                    inner = r2.get("data",{})
                    if isinstance(inner, dict) and "data" in inner:
                        inner = inner["data"]
                    data = inner.get(dhan_seg,{}) if isinstance(inner,dict) else {}
                    for k,v in data.items():
                        ltp = _f(v.get("last_price",0))
                        if ltp > 0: und = ltp; break
                except Exception as ex:
                    print(f"[ltp] {symbol} err: {ex}", file=sys.stderr)
                if und > 0: break

            print(f"[scan] {symbol}: und=₹{und}", file=sys.stderr)

            opts = [r for r in sym_rows if r.get("SEM_OPTION_TYPE","") in ("CE","PE")]
            if opts:
                exp_dates = sorted(set(r.get("SEM_EXPIRY_DATE","")[:10] for r in opts))
                keep_exps = set(exp_dates[:2])
                opts = [r for r in opts if r.get("SEM_EXPIRY_DATE","")[:10] in keep_exps]
                if und > 0:
                    opts.sort(key=lambda r: abs(float(r.get("SEM_STRIKE_PRICE",0)) - und))
                # FIX: always cap options — outside the if und>0 block
                opts = opts[:OPTS_PER_SYM]
                for r in opts:
                    sec_id = r["SEM_SMST_SECURITY_ID"]
                    tsym   = r["SEM_TRADING_SYMBOL"]
                    exp    = r.get("SEM_EXPIRY_DATE","?")[:10]
                    sk     = r.get("SEM_STRIKE_PRICE","0")
                    otype  = r.get("SEM_OPTION_TYPE","")
                    candidates[cat].append({
                        "tok": sec_id, "seg": dhan_seg, "sym": tsym,
                        "type": otype, "strike": int(float(sk)) if sk else 0,
                        "expiry": exp, "symbol": symbol
                    })

    quotes = {}
    for cat, conts in candidates.items():
        # Dedupe by security_id (globally unique on Dhan) — the same contract
        # can be reached via overlapping symbol/expiry passes. Preserve order.
        seen = set()
        deduped = []
        for c in conts:
            k = str(c["tok"])
            if k in seen:
                continue
            seen.add(k)
            deduped.append(c)
        conts = deduped
        token_map[cat] = conts
        futs = [c for c in conts if c["type"] == "FUT"]
        if not futs: continue
        try:
            by_seg = {}
            for c in futs:
                by_seg.setdefault(c["seg"],[]).append(int(c["tok"]))
            r2 = dhan.ohlc_data(securities=by_seg)
            inner = r2.get("data",{})
            if isinstance(inner,dict) and "data" in inner:
                inner = inner["data"]
            for seg_k, seg_data in (inner.items() if isinstance(inner,dict) else {}.items()):
                for sid, qdata in (seg_data.items() if isinstance(seg_data,dict) else {}.items()):
                    quotes[str(sid)] = {"ltp": _f(qdata.get("last_price",0))}
        except Exception as ex:
            print(f"[quotes] {cat} err: {ex}", file=sys.stderr)

    total = sum(len(v) for v in token_map.values())
    print(f"[done] total {total} tokens, {len(quotes)} quotes", file=sys.stderr)
    result = {"session": {}, "token_map": token_map, "quotes": quotes, "diag": {}, "ck": client_id}
    print(json.dumps(result))
    sys.stdout.flush()

if __name__ == "__main__":
    main()
