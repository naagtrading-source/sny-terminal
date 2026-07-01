"""
daily_ad_helper.py — daily-timeframe accumulation/distribution scan.
Reads FUT tokens from stdin (JSON list of {"tok","seg","sym"}), fetches
~50 days of daily candles per contract, flags contracts where today's
volume is the highest in >=10 days, tagged accumulation/distribution by
close-vs-open direction. Emits JSON to stdout.
"""
import os, sys, json, time, datetime

MIN_RANK_DAYS = 10   # today must be highest volume in >= this many days to flag

def _f(v):
    try: return float(str(v).replace(",",""))
    except: return 0.0

def main():
    raw_in = sys.stdin.read().strip()
    if not raw_in:
        print(json.dumps({"error":"no_input"})); return
    toks = json.loads(raw_in)
    if not toks:
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

    today = datetime.date.today()
    frm = (today - datetime.timedelta(days=50)).strftime("%Y-%m-%d")
    to  = today.strftime("%Y-%m-%d")

    hits = []
    for t in toks:
        sym = t.get("sym","?")
        try:
            r = dhan.historical_daily_data(
                security_id=str(t["tok"]),
                exchange_segment=t.get("seg","NSE_FNO"),
                instrument_type="FUTSTK",
                from_date=frm, to_date=to,
            )
            if not (isinstance(r, dict) and r.get("status") == "success"):
                continue
            d = r.get("data", {})
            vols  = d.get("volume", []) or []
            opens = d.get("open", []) or []
            closes= d.get("close", []) or []
            if len(vols) < MIN_RANK_DAYS + 1:
                continue
            today_vol = _f(vols[-1])
            if today_vol <= 0:
                continue
            # how many days back is today's volume the highest?
            rank_days = 1
            for i in range(len(vols) - 2, -1, -1):
                if _f(vols[i]) < today_vol:
                    rank_days += 1
                else:
                    break
            if rank_days < MIN_RANK_DAYS:
                continue
            # 30-day avg (excluding today) for context
            window = [_f(x) for x in vols[-31:-1]] or [_f(x) for x in vols[:-1]]
            avg = sum(window) / len(window) if window else 0.0
            x_avg = round(today_vol / avg, 1) if avg > 0 else 0.0
            # direction
            o = _f(opens[-1]); c = _f(closes[-1])
            chg_pct = round((c - o) / o * 100, 2) if o > 0 else 0.0
            if c > o:
                direction, emoji = "ACCUMULATION", "🟢"
            elif c < o:
                direction, emoji = "DISTRIBUTION", "🔴"
            else:
                direction, emoji = "ABSORPTION", "🟡"
            hits.append({
                "sym": sym,
                "rank_days": rank_days,
                "x_avg": x_avg,
                "today_vol": int(today_vol),
                "direction": direction,
                "emoji": emoji,
                "chg_pct": chg_pct,
                "close": round(c, 2),
            })
        except Exception as e:
            print(f"[ad] {sym} err: {e}", file=sys.stderr)
        time.sleep(0.1)  # throttle for rate limits

    hits.sort(key=lambda h: h["rank_days"], reverse=True)
    print(json.dumps({"hits": hits}))
    sys.stdout.flush()

if __name__ == "__main__":
    main()
