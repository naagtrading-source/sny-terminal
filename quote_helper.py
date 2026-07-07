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
    import time as _time
    ctx  = DhanContext(client_id, access_token)
    dhan = dhanhq(ctx)
    # Budget to stop the bisect from spiraling on bad/empty tokens at open.
    _budget = {"calls": 0, "max_calls": 120, "deadline": _time.time() + 70}
    def _over_budget():
        return _budget["calls"] >= _budget["max_calls"] or _time.time() > _budget["deadline"]

    by_seg = {}
    for t in tokens:
        by_seg.setdefault(t["seg"],[]).append(int(t["tok"]))

    quotes = {}
    # Dhan limits: <=50 instruments per segment per call, <=1000 total.
    def _batches(bs):
        # One segment per batch — a failing segment (e.g. NSE when closed)
        # must never knock out a live segment (MCX) sharing the same call.
        for k, v in bs.items():
            ids = list(v)
            for i in range(0, len(ids), 50):
                yield {k: ids[i:i+50]}

    def _fetch(bseg, depth=0):
        # Dhan fails an ENTIRE batch if any one security_id is bad/expired.
        # On failure, bisect down to per-token so one bad ID drops only itself.
        # Budget guard: if we've spent our call/time budget, stop retrying —
        # skip remaining bad tokens this cycle rather than spiral into a timeout.
        if _over_budget():
            return {"status": "budget_exhausted"}
        _budget["calls"] += 1
        r = dhan.quote_data(securities=bseg)
        ok = isinstance(r, dict) and r.get("status") == "success"
        if ok:
            return r
        # split and retry
        seg_k = next(iter(bseg))
        ids = bseg[seg_k]
        if len(ids) <= 1 or depth > 6:
            return r  # single bad token (or too deep) — give up on it
        mid = len(ids) // 2
        merged = {"status": "success", "data": {"data": {}}}
        for half in (ids[:mid], ids[mid:]):
            if not half:
                continue
            rr = _fetch({seg_k: half}, depth + 1)
            if isinstance(rr, dict) and rr.get("status") == "success":
                inner = rr.get("data", {})
                inner = inner.get("data", {}) if isinstance(inner, dict) else {}
                if isinstance(inner, dict):
                    for sk, sd in inner.items():
                        merged["data"]["data"].setdefault(sk, {}).update(sd or {})
        return merged

    for bseg in _batches(by_seg):
        if _over_budget():
            break
        try:
            r = _fetch(bseg)
            if not isinstance(r, dict):
                continue
            data = r.get("data", {})
            if isinstance(data, dict) and "data" in data:
                data = data["data"]
            if not isinstance(data, dict):
                continue
            for seg_k, seg_data in data.items():
                if not isinstance(seg_data, dict):
                    continue
                for sid, qdata in seg_data.items():
                    if not isinstance(qdata, dict):
                        continue
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
        except Exception:
            continue  # skip this batch, keep others

    print(json.dumps({"quotes": quotes}))
    sys.stdout.flush()

if __name__ == "__main__":
    main()
