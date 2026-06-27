"""
quote_helper.py — Dhan-based fast quote refresher.
"""
import os, sys, json

def _f(v):
    try: return float(str(v).replace(",",""))
    except: return 0.0

def main():
    raw_in = sys.stdin.read().strip()
    if not raw_in:
        print(json.dumps({"error":"no_input"})); return
    tokens = json.loads(raw_in)
    if not tokens:
        print(json.dumps({"error":"empty_tokens"})); return

    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    except Exception: pass
    client_id    = os.environ["DHAN_CLIENT_ID"].strip()
    access_token = os.environ["DHAN_ACCESS_TOKEN"].strip()

    from dhanhq import DhanContext, dhanhq
    ctx  = DhanContext(client_id, access_token)
    dhan = dhanhq(ctx)

    by_seg = {}
    for t in tokens:
        by_seg.setdefault(t["seg"],[]).append(int(t["tok"]))

    quotes = {}
    try:
        r = dhan.get_quote_data(securities=by_seg)
        data = r.get("data", {})
        for seg_k, seg_data in data.items():
            for sid, qdata in seg_data.items():
                quotes[str(sid)] = {
                    "ltp":                  _f(qdata.get("last_price", 0)),
                    "last_volume":          int(_f(qdata.get("volume", 0))),
                    "open_int":             int(_f(qdata.get("oi", 0))),
                    "last_traded_quantity": int(_f(qdata.get("last_quantity", 0))),
                    "total_buy":            int(_f(qdata.get("buy_quantity", 0))),
                    "total_sell":           int(_f(qdata.get("sell_quantity", 0))),
                    "change":               _f(qdata.get("net_change", 0)),
                    "per_change":           _f(qdata.get("percentage_change", 0)),
                }
    except Exception as ex:
        print(json.dumps({"error": str(ex)})); return

    print(json.dumps({"quotes": quotes}))
    sys.stdout.flush()

if __name__ == "__main__":
    main()
