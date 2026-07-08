"""
gold_helper.py — stateless gold-block retest scan (15m, Nifty-50).
Recomputes blocks from history each call (history IS the memory).
Prints JSON: {"alerts": [ {...}, ... ]}. Dedup is handled by the daemon.
"""
import os, sys, json, datetime as dt
from collections import OrderedDict

def main():
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    except Exception:
        pass
    try:
        from dhanhq import DhanContext, dhanhq
        from ob_score import Bar, score_block
        from gold_retest import aggregate_1m, GoldRetestDetector, close_pos_delta, NIFTY50, INDICES, mcx_front_month
    except Exception as e:
        print(json.dumps({"alerts": [], "err": f"import: {e}"}))
        return

    client_id    = os.environ["DHAN_CLIENT_ID"].strip()
    access_token = os.environ["DHAN_ACCESS_TOKEN"].strip()
    ctx  = DhanContext(client_id, access_token)
    dhan = dhanhq(ctx)
    IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

    to  = dt.date.today().isoformat()
    frm = (dt.date.today() - dt.timedelta(days=25)).isoformat()

    det = GoldRetestDetector()
    alerts = []
    formations = []
    import time
    import sys as _sys
    _args = set(_sys.argv[1:])
    _do_nse = ("--nse" in _args) or (not _args)   # default: scan NSE if no flags
    _do_mcx = ("--mcx" in _args)

    scan_list = []
    if _do_nse:
        scan_list += [(sym, sid, "NSE_EQ", "EQUITY") for sym, sid in NIFTY50.items()]
        scan_list += [(sym, ix["id"], ix["seg"], ix["inst"]) for sym, ix in INDICES.items()]
    if _do_mcx:
        # keep the scrip master fresh (commodity contracts roll monthly)
        try:
            import os as _os, time as _t, urllib.request as _u
            _mp = _os.path.join(_os.path.dirname(__file__), "api-scrip-master-fresh.csv")
            if (not _os.path.exists(_mp)) or (_t.time() - _os.path.getmtime(_mp) > 86400):
                _rq = _u.Request("https://images.dhan.co/api-data/api-scrip-master.csv",
                                 headers={"User-Agent": "Mozilla/5.0"})
                open(_mp, "w").write(_u.urlopen(_rq, timeout=60).read().decode("utf-8", "ignore"))
        except Exception:
            pass  # if refresh fails, mcx_front_month falls back to existing file
        for _base, _sid in mcx_front_month().items():
            scan_list.append((_base, _sid, "MCX_COMM", "FUTCOM"))
    for sym, sid, seg, inst in scan_list:
        try:
            r = dhan.intraday_minute_data(security_id=str(sid),
                                          exchange_segment=seg,
                                          instrument_type=inst,
                                          from_date=frm, to_date=to, interval=1)
            time.sleep(0.15)
            if not isinstance(r, dict):
                continue
            d = r.get("data") or {}
            o=d.get("open") or []; h=d.get("high") or []; l=d.get("low") or []
            c=d.get("close") or []; v=d.get("volume") or []; ts=d.get("timestamp") or []
            n = min(len(o),len(h),len(l),len(c),len(v),len(ts))
            if n < 200:
                continue
            days = OrderedDict()
            for i in range(n):
                day = dt.datetime.fromtimestamp(ts[i], IST).date()
                days.setdefault(day, []).append(
                    Bar(float(o[i]),float(h[i]),float(l[i]),float(c[i]),float(v[i])))
            bars15 = []
            for _day, b1 in days.items():
                bars15 += aggregate_1m(b1, 15)
            if len(bars15) < 125:
                continue
            last_ts = int(ts[n-1])
            # feed the whole series so blocks are (re)built, then check the last bar
            det._blocks.pop(f"{sym}|15m", None)
            for i in range(125, len(bars15)+1):
                a = det.update(sym, "15m", bars15[:i], i)
                if a and i == len(bars15):
                    alerts.append({
                        "symbol": a.symbol, "tf": a.tf, "dir": a.direction,
                        "grade": a.grade, "score": a.score,
                        "top": round(a.zone_top,2), "bot": round(a.zone_bot,2),
                        "price": round(a.price,2), "buy_pct": a.buy_pct,
                        "vol_x": a.vol_x, "zkey": f"{sym}|{a.direction}|{round(a.zone_bot,1)}",
                    })
            # formation: did the NEWEST bar itself just form a gold A/A+ block?
            # score_block only returns when a BOS occurs ON the last bar, so this
            # is inherently "fresh". Dedup key uses the 15m bar's close timestamp
            # (stable within a candle, changes only when a NEW candle closes) plus
            # a COARSE zone bucket (0.1% of price) so tiny per-scan zone drift
            # cannot defeat dedup and cause repeat alerts.
            from ob_score import score_block as _sb
            _blk = _sb(bars15, trend_len=120, range_len=50, vol_mult=2.0, min_score=75)
            if _blk is not None and _blk.score >= 75:
                formations.append({
                    "symbol": sym, "tf": "15m", "dir": _blk.direction,
                    "grade": _blk.grade, "score": _blk.score,
                    "top": round(_blk.top,2), "bot": round(_blk.bot,2),
                    "fkey": f"{sym}|{_blk.direction}|{round(_blk.bot,1)}|{round(_blk.top,1)}",
                })
        except Exception:
            continue

    print(json.dumps({"alerts": alerts, "formations": formations}))

if __name__ == "__main__":
    main()
