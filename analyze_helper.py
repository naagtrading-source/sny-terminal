"""
analyze_helper.py — Dhan-based single-symbol analyzer.
"""
import os, sys, json, re, csv, urllib.request, datetime
import pytz

def _f(v):
    try: return float(str(v).replace(",",""))
    except: return 0.0

def main():
    raw_in = sys.stdin.read().strip()
    if not raw_in:
        print(json.dumps({"error":"no_input"})); return
    params = json.loads(raw_in)
    symbol = params["symbol"].upper().strip()
    seg    = params.get("exchange","nse_fo")

    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    except Exception: pass
    client_id    = os.environ["DHAN_CLIENT_ID"].strip()
    access_token = os.environ["DHAN_ACCESS_TOKEN"].strip()

    from dhanhq import DhanContext, dhanhq
    ctx  = DhanContext(client_id, access_token)
    dhan = dhanhq(ctx)

    dhan_seg = "NSE_FNO" if seg == "nse_fo" else "MCX"
    exch     = "NSE" if seg == "nse_fo" else "MCX"

    url = "https://images.dhan.co/api-data/api-scrip-master.csv"
    with urllib.request.urlopen(url, timeout=30) as resp:
        lines = resp.read().decode("utf-8").splitlines()
    master = list(csv.DictReader(lines))

    today = datetime.datetime.now(pytz.timezone("Asia/Kolkata")).date()
    sym_rows = []
    for row in master:
        if row.get("SEM_EXM_EXCH_ID","") != exch: continue
        tsym = row.get("SEM_TRADING_SYMBOL","")
        if not (tsym.upper().startswith(symbol+"-") or tsym.upper().startswith(symbol+" ")): continue
        try:
            exp_d = datetime.datetime.strptime(row.get("SEM_EXPIRY_DATE","").strip()[:10], "%Y-%m-%d").date()
            if exp_d < today: continue
        except: pass
        sym_rows.append(row)

    if not sym_rows:
        print(json.dumps({"error":f"No contracts for {symbol}","contracts":[]})); return

    futs = sorted([r for r in sym_rows if "FUT" in r.get("SEM_INSTRUMENT_NAME","").upper()], key=lambda r: r.get("SEM_EXPIRY_DATE",""))
    opts = [r for r in sym_rows if r.get("SEM_OPTION_TYPE","") in ("CE","PE")]

    und = 0.0
    results = []

    for r in futs[:3]:
        sec_id = r["SEM_SMST_SECURITY_ID"]
        tsym   = r["SEM_TRADING_SYMBOL"]
        try:
            qr    = dhan.get_quote_data(securities={dhan_seg: [int(sec_id)]})
            qdata = qr.get("data",{}).get(dhan_seg,{}).get(str(sec_id), qr.get("data",{}).get(dhan_seg,{}).get(sec_id,{}))
            ltp   = _f(qdata.get("last_price",0))
            vol   = int(_f(qdata.get("volume",0)))
            oi    = int(_f(qdata.get("oi",0)))
            ltq   = int(_f(qdata.get("last_quantity",0)))
            tb    = int(_f(qdata.get("buy_quantity",0)))
            ts_   = int(_f(qdata.get("sell_quantity",0)))
            chg   = _f(qdata.get("net_change",0))
            pchg  = _f(qdata.get("percentage_change",0))
            if ltp > 0 and und == 0: und = ltp
            side  = "BUY-heavy" if tb > ts_*1.2 else "SELL-heavy" if ts_ > tb*1.2 else "balanced"
            results.append({"contract":tsym,"type":"FUT","strike":"-","ltp":ltp,
                "volume":vol,"oi":oi,"ltq":ltq,"change":chg,"pct_change":pchg,
                "buy_qty":tb,"sell_qty":ts_,"side":side})
        except: pass

    if und > 0:
        opts.sort(key=lambda r: abs(float(r.get("SEM_STRIKE_PRICE",0))-und))
    opts = opts[:20]

    for r in opts:
        sec_id = r["SEM_SMST_SECURITY_ID"]
        tsym   = r["SEM_TRADING_SYMBOL"]
        sk     = r.get("SEM_STRIKE_PRICE","0")
        otype  = r.get("SEM_OPTION_TYPE","")
        try:
            qr    = dhan.get_quote_data(securities={dhan_seg: [int(sec_id)]})
            qdata = qr.get("data",{}).get(dhan_seg,{}).get(str(sec_id), qr.get("data",{}).get(dhan_seg,{}).get(sec_id,{}))
            ltp   = _f(qdata.get("last_price",0))
            vol   = int(_f(qdata.get("volume",0)))
            oi    = int(_f(qdata.get("oi",0)))
            ltq   = int(_f(qdata.get("last_quantity",0)))
            tb    = int(_f(qdata.get("buy_quantity",0)))
            ts_   = int(_f(qdata.get("sell_quantity",0)))
            chg   = _f(qdata.get("net_change",0))
            pchg  = _f(qdata.get("percentage_change",0))
            side  = "BUY-heavy" if tb > ts_*1.2 else "SELL-heavy" if ts_ > tb*1.2 else "balanced"
            results.append({"contract":tsym,"type":otype,"strike":int(float(sk)) if sk else 0,
                "ltp":ltp,"volume":vol,"oi":oi,"ltq":ltq,"change":chg,"pct_change":pchg,
                "buy_qty":tb,"sell_qty":ts_,"side":side})
        except: pass

    results = [r for r in results if r["volume"] > 0 or r["ltp"] > 0]
    results.sort(key=lambda r: r["volume"], reverse=True)
    print(json.dumps({"symbol":symbol,"exchange":seg,"underlying":und,
        "total_contracts":len(sym_rows),"analyzed":len(results),"results":results}))
    sys.stdout.flush()

if __name__ == "__main__":
    main()
