"""
SNY Block Detector — Single Render Service
==========================================
NEVER imports neo_api_client directly.
Auth runs in a short-lived subprocess (auth_helper.py) which loads
the heavy SDK, gets tokens, writes JSON to stdout, then EXITS.
Main process stays light (~120MB) using only requests for live quotes.
"""
import streamlit as st
import pandas as pd
import os, json, re, subprocess, sys, gc, time
import requests as _req
from datetime import datetime
from collections import defaultdict

# ── Telegram alerts ───────────────────────────────────────────────────────────
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "")

def send_telegram(block):
    """Send an unusual activity alert to Telegram."""
    if not TG_TOKEN or not TG_CHAT: return
    try:
        # Build clean contract label
        if block["type"] == "FUT":
            label = f"{block['symbol']} FUT"
        else:
            label = f"{block['symbol']} {block['strike']} {block['type']}"

        emoji = block.get("emoji", "")
        activity = block.get("activity", "")
        bias = block.get("bias", "")
        acc = block.get("acc_dist", "")

        lines = [
            f"{emoji} *{activity}* — {label}",
            f"₹{block['ltp']:,}  (Δ{block.get('price_chg',0):+.1f})",
            f"Vol: {block['total_vol']:,}  (Δ{block['vol_jump']:+,})",
        ]
        if block.get('vol_mult', 0) > 0:
            lines.append(f"Vol vs Avg: {block['vol_mult']:.1f}×")
        lines.append(f"OI Δ: {block['oi_chg_pct']:+.0f}%")
        lines.append(f"Side: {block.get('side_emoji','')} {block.get('side','')}")
        if acc:
            lines.append(f"🔇 {block.get('acc_emoji','')} {acc}")
        lines.append(f"Signal: {block.get('reasons','')}")
        lines.append(f"⏰ {block['time']}")

        msg = "\n".join(lines)
        _req.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=5,
        )
    except: pass  # don't let Telegram errors break the app

# Import external packages separately so a missing one is obvious
try:
    import requests
except ImportError:
    st.error("Missing package: requests — add 'requests' to requirements.txt")
    st.stop()
try:
    import pytz
except ImportError:
    st.error("Missing package: pytz — add 'pytz' to requirements.txt")
    st.stop()

st.set_page_config(page_title="SNY Block Detector", layout="wide", page_icon="⚡")
st.markdown("""
<style>
body,.stApp{background:#0d1117;color:#e6edf3}
div[data-testid="stVerticalBlock"]{gap:0.4rem!important}
.stTabs [data-baseweb="tab-list"]{gap:6px;background:#161b22;padding:6px;border-radius:8px}
.stTabs [data-baseweb="tab"]{background:#21262d!important;color:#8b949e!important;
  border-radius:6px;padding:8px 20px;font-weight:600;border:1px solid #30363d!important}
.stTabs [aria-selected="true"]{background:#1f6feb!important;color:#fff!important;
  font-weight:700!important;border:1px solid #388bfd!important}
div[data-testid="metric-container"]{background:#161b22;border:1px solid #30363d;
  border-radius:8px;padding:10px 14px}
</style>""", unsafe_allow_html=True)

IST = pytz.timezone("Asia/Kolkata")

# ── Config ─────────────────────────────────────────────────────────────────────
CATEGORIES = {
    "Index":    ["NIFTY","BANKNIFTY"],
    "Stock":    ["RELIANCE","HDFCBANK","TCS","INFY","ICICIBANK","SBIN"],
    "Commodity":["GOLDM","SILVERM","CRUDEOIL","NATURALGAS","COPPER"],
}
LOTS = {
    "NIFTY":75,"BANKNIFTY":30,"FINNIFTY":40,"MIDCPNIFTY":75,
    "RELIANCE":250,"HDFCBANK":550,"TCS":175,"INFY":400,"ICICIBANK":700,"SBIN":1500,
    "GOLDM":10,"SILVERM":5,"CRUDEOIL":100,"NATURALGAS":1250,"COPPER":2500,
}
# Thresholds — ONLY institutional-level activity (very high bar)
VOL_SPIKE_MULT = 5.0       # volume jump must be > 5x this contract's own average
MIN_VOL_JUMP   = 50000     # ignore jumps under 50k (institutional = large)
LARGE_VALUE_CR = 5.0       # value of jump must exceed Rs 5 crore
OI_CHANGE_PCT  = 15.0      # OI change > 15% = significant new institutional positions
BIG_TRADE_LOTS = 50        # single trade >= 50 lots = block print
MIN_HISTORY    = 3         # need at least 3 ticks of history before flagging unusual

def interpret_activity(opt_type, oi_change, price_change):
    """
    Decode institutional intent from OI + price direction.
    Returns (label, emoji, bias) describing what smart money is doing.
    For options:
      OI↑ Price↑ = fresh buying (conviction)
      OI↑ Price↓ = fresh writing (selling premium / capping)
      OI↓ Price↑ = short covering
      OI↓ Price↓ = long unwinding
    """
    if opt_type == "CE":
        if oi_change > 0 and price_change > 0:
            return ("CALL BUYING", "🟢📈", "BULLISH")      # bullish bet
        if oi_change > 0 and price_change < 0:
            return ("CALL WRITING", "🔴✍️", "BEARISH")     # resistance/capping
        if oi_change < 0 and price_change > 0:
            return ("CALL SHORT COVER", "🟡", "BULLISH")
        if oi_change < 0 and price_change < 0:
            return ("CALL LONG UNWIND", "🟠", "BEARISH")
    elif opt_type == "PE":
        if oi_change > 0 and price_change > 0:
            return ("PUT BUYING", "🔴📉", "BEARISH")       # bearish bet
        if oi_change > 0 and price_change < 0:
            return ("PUT WRITING", "🟢✍️", "BULLISH")      # support
        if oi_change < 0 and price_change > 0:
            return ("PUT SHORT COVER", "🟡", "BEARISH")
        if oi_change < 0 and price_change < 0:
            return ("PUT LONG UNWIND", "🟠", "BULLISH")
    else:  # FUT
        if oi_change > 0 and price_change > 0: return ("LONG BUILDUP", "🟢📈", "BULLISH")
        if oi_change > 0 and price_change < 0: return ("SHORT BUILDUP", "🔴📉", "BEARISH")
        if oi_change < 0 and price_change > 0: return ("SHORT COVERING", "🟡", "BULLISH")
        if oi_change < 0 and price_change < 0: return ("LONG UNWINDING", "🟠", "BEARISH")
    return ("NEUTRAL", "⚪", "NEUTRAL")

# ── AUTH via subprocess — SDK loads/runs/exits, freeing its memory ─────────────
# Background auth: run subprocess in a thread so the main script never blocks
# the 60s health check. Result is stored and polled.
@st.cache_resource
def _auth_holder():
    return {"done":False,"result":None,"thread":None,"ts":0}

def _run_auth_bg(holder):
    try:
        env = {**os.environ, "PYTHONUNBUFFERED":"1"}
        proc = subprocess.run(
            [sys.executable, "auth_helper.py"],
            capture_output=True, text=True, timeout=240, env=env,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            holder["result"]=(None,{},{},{"stderr":proc.stderr[-800:]},f"auth failed: {proc.stderr[-200:]}")
        else:
            data=json.loads(proc.stdout.strip())
            if "error" in data:
                holder["result"]=(None,{},{},{"stderr":proc.stderr[-800:]},f"auth error: {data['error']}")
            else:
                d=data.get("diag",{})
                d["stderr"]=proc.stderr[-800:]
                holder["result"]=(data.get("session",{}),data.get("token_map",{}),
                                  data.get("quotes",{}),d,None)
    except Exception as e:
        holder["result"]=(None,{},{},{},str(e))
    holder["done"]=True
    holder["ts"]=time.time()
    gc.collect()

CONFIG_VERSION = "v23-analyzer"

def get_auth():
    """
    Non-blocking auth. NOT cached (the holder IS cached).
    Checks the persistent holder each call. Starts background thread on first call.
    Re-auths every 20 minutes via timestamp check.
    """
    import threading
    holder=_auth_holder()

    # If done and fresh (within 20 min), return result immediately
    if holder["done"] and holder["result"] and holder["ts"] > 0:
        age = time.time() - holder["ts"]
        if age < 1200:
            return holder["result"]
        # Expired — reset for fresh auth
        holder["done"] = False
        holder["result"] = None
        holder["thread"] = None

    # Start auth in background if not already running
    if holder["thread"] is None or (not holder["thread"].is_alive() and not holder["done"]):
        holder["done"] = False
        holder["thread"]=threading.Thread(target=_run_auth_bg,args=(holder,),daemon=True)
        holder["thread"].start()

    # Wait briefly (under 60s health-check limit) then return whatever we have
    if holder["thread"] and holder["thread"].is_alive():
        holder["thread"].join(timeout=2)  # short wait, don't block

    if holder["done"] and holder["result"]:
        return holder["result"]

    # Not done yet — return "connecting" state; rerun will pick up result
    return (None,{},{},{},"connecting")

session, token_map, sdk_quotes, sdk_diag, auth_err = get_auth()

# If auth still connecting in background, auto-rerun to poll for result
if auth_err == "connecting":
    import time as _t
    _t.sleep(3)
    st.rerun()

def fetch_quotes_fast():
    """
    Spawn quote_helper.py which reuses the saved session (no login) to fetch
    fresh quotes quickly. Returns {token: quote_dict} or {} on failure.
    Runs in a thread-safe subprocess; takes ~3-5s vs ~60s for full auth.
    """
    if not token_map:
        return {}
    # Build token list from token_map
    toks=[]
    for sym,entries in token_map.items():
        for e in entries:
            if e.get("tok"):
                toks.append({"tok":e["tok"],"seg":e["seg"]})
    if not toks:
        return {}
    try:
        proc = subprocess.run(
            [sys.executable, "quote_helper.py"],
            input=json.dumps(toks),
            capture_output=True, text=True,
            timeout=40, env={**os.environ, "PYTHONUNBUFFERED":"1"},
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return {}
        data=json.loads(proc.stdout.strip())
        if "error" in data:
            # Session expired or missing — clear auth cache to force re-login next time
            if data["error"] in ("no_session","session_load"):
                get_auth.clear()
            return {}
        gc.collect()
        return data.get("quotes",{})
    except Exception:
        return {}

# ── Build session headers from whatever the subprocess found ───────────────────
def build_headers(session, ck):
    """Try to reconstruct valid session headers from captured api attributes."""
    auth  = (session.get("cfg_auth") or session.get("cfg_token") or
             session.get("cfg_access_token") or session.get("auth") or
             session.get("token") or session.get("access_token") or "")
    sid   = (session.get("cfg_sid") or session.get("sid") or "")
    return {
        "accept":        "application/json",
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {ck}",
        "Auth":          auth,
        "sid":           sid,
        "Sid":           sid,
        "neo-fin-key":   f"neotradeapi{sid}",
    }

ck = (session or {}).get("ck", os.environ.get("KOTAK_CONSUMER_KEY",""))

# ── Live quote from SDK-fetched quotes dict (passed by subprocess) ─────────────
def live_quote(token, seg=None):
    """Look up the pre-fetched quote for this token."""
    if not token: return {}
    return sdk_quotes.get(str(token), {})

# ── Field extractors ──────────────────────────────────────────────────────────
def _f(v):
    try: return float(str(v).replace(",",""))
    except: return 0.0
def _ltp(q):
    if not isinstance(q,dict): return 0.0
    for k in ("ltp","last_price","last_traded_price","lastPrice","LTP","close"):
        v=q.get(k)
        if v not in (None,"",0,"0",0.0):
            f=_f(v)
            if f>0: return f
    return 0.0
def _vol(q):
    if not isinstance(q,dict): return 0
    for k in ("last_volume","volume","vol","volume_traded","totalTradedVolume"):
        v=q.get(k)
        if v not in (None,""): 
            try: return max(0,int(_f(v)))
            except: pass
    return 0
def _oi(q):
    if not isinstance(q,dict): return 0
    for k in ("open_int","open_interest","oi","openInterest","OI"):
        v=q.get(k)
        if v not in (None,""):
            try: return max(0,int(_f(v)))
            except: pass
    return 0
def _ltq(q):
    if not isinstance(q,dict): return 0
    for k in ("last_traded_quantity","ltq","lastTradedQty","ltSize"):
        v=q.get(k)
        if v not in (None,""):
            try: return max(0,int(_f(v)))
            except: pass
    return 0
def _trend(q,opt):
    bq=int(_f(q.get("total_buy",q.get("total_buy_quantity",q.get("buyQty",0)))or 0))
    sq=int(_f(q.get("total_sell",q.get("total_sell_quantity",q.get("sellQty",0)))or 0))
    if bq>0 and sq>0:
        if bq>sq*1.2: return "🟢 BUY"
        if sq>bq*1.2: return "🔴 SELL"
        return "⚪ NEUT"
    return "🟢 BULL" if opt=="CE" else "🔴 BEAR"

# ── Volume history & prev state ────────────────────────────────────────────────
# Persistent store — survives page reloads (cache_resource is shared/persistent)
@st.cache_resource
def _persistent_store():
    return {"prev": defaultdict(dict), "volhist": defaultdict(list), "feed": [], "snapshot": []}

_STORE = _persistent_store()
# Mirror into session_state keys for compatibility with existing code
st.session_state["prev"]    = _STORE["prev"]
st.session_state["volhist"] = _STORE["volhist"]
st.session_state["feed"]    = _STORE["feed"]

def vh_avg(key):
    h=st.session_state["volhist"].get(key,[])
    return sum(h[:-1])/len(h[:-1]) if len(h)>=3 else 0

# ── INSTITUTIONAL ACTIVITY DETECTION ──────────────────────────────────────────
def detect_blocks():
    ts  = datetime.now(IST).strftime("%H:%M:%S")
    new = []
    for cat, entries in token_map.items():
        for entry in entries:
            tok  = entry.get("tok")
            if not tok: continue
            seg  = entry["seg"]; kind = entry["type"]
            sk   = entry.get("strike"); exp = entry.get("expiry","")
            sym  = entry.get("sym",""); symbol = entry.get("symbol", sym)
            lot  = LOTS.get(symbol, 100)

            q   = live_quote(tok, seg)
            ltp = _ltp(q); vol = _vol(q); oi = _oi(q); ltq = _ltq(q)
            if vol <= 0 and ltp <= 0: continue

            ikey = f"{symbol}|{kind}|{sk}|{exp}"
            h = st.session_state["volhist"][ikey]
            h.append(vol)
            if len(h) > 15: h.pop(0)

            prev      = st.session_state["prev"].get(ikey, {})
            prev_vol  = prev.get("vol", vol)
            prev_oi   = prev.get("oi",  oi)
            prev_ltp  = prev.get("ltp", ltp)
            vol_jump  = vol - prev_vol
            avg       = vh_avg(ikey)
            oi_chg    = oi  - prev_oi
            oi_pct    = (oi_chg/prev_oi*100) if prev_oi > 0 else 0
            price_chg = ltp - prev_ltp

            # ── UNUSUAL activity gate — must be abnormal vs THIS contract's norm ──
            is_unusual = False; reasons = []

            # ── Interpretation (always computed, shown beside each) ──────────
            label, emoji, bias = interpret_activity(kind, oi_chg, price_chg)
            # Buy/sell pressure as a secondary read
            bq = int(_f(q.get("total_buy", 0) or 0))
            sq = int(_f(q.get("total_sell", 0) or 0))
            pressure = "BUY-led" if bq > sq*1.2 else "SELL-led" if sq > bq*1.2 else "balanced"

            # ── Flags for "unusual" — need MIN_HISTORY ticks before flagging ────
            flags = []
            has_history = len(h) >= MIN_HISTORY  # don't flag on first few ticks
            # OI sanity: changes > 50% in one tick are comparison artifacts, not real
            oi_sane = abs(oi_pct) < 50
            # Skip if nothing actually traded since last tick
            prev_ltq = prev.get("ltq", 0)

            if has_history and avg > 0 and vol_jump >= MIN_VOL_JUMP and vol_jump >= avg * VOL_SPIKE_MULT:
                is_unusual = True
                flags.append(f"Vol {vol_jump/avg:.1f}× normal")
            if has_history and oi_sane and abs(oi_pct) >= OI_CHANGE_PCT and prev_oi > 0 and vol_jump > 0:
                is_unusual = True
                flags.append(f"OI {oi_pct:+.0f}%")
            if ltq >= lot * BIG_TRADE_LOTS and ltq > 0 and ltq != prev_ltq and vol_jump > 0:
                is_unusual = True
                flags.append(f"Block {ltq:,}")

            value_cr = (vol * ltp) / 1e7   # total traded value (turnover)
            jump_cr  = (vol_jump * ltp) / 1e7

            # ── BUYING or SELLING? ───────────────────────────────────────────
            # Combine price direction (with volume) + order-book pressure.
            # Aggressive buying: price up while volume surges, buy-side heavier.
            # Aggressive selling: price down while volume surges, sell-side heavier.
            buy_score = 0
            if price_chg > 0: buy_score += 1
            if price_chg < 0: buy_score -= 1
            if bq > sq*1.2:   buy_score += 1
            if sq > bq*1.2:   buy_score -= 1
            if   buy_score >= 1:  side, side_emoji = "BUYING",  "🟢"
            elif buy_score <= -1: side, side_emoji = "SELLING", "🔴"
            else:                 side, side_emoji = "MIXED",   "⚪"
            # Volume vs regular (how many times its normal)
            vol_mult = (vol_jump/avg) if avg > 0 else 0

            # ── ACCUMULATION / DISTRIBUTION (silent institutional absorption) ──
            # Signature: price barely moves (tight range) BUT volume is huge AND
            # OI is building. Big players absorbing supply/demand without moving price.
            acc_dist = ""; acc_emoji = ""
            price_pct = abs(price_chg / ltp * 100) if ltp > 0 else 0
            is_flat = price_pct < 0.5          # price moved less than 0.5%
            huge_vol = vol_mult >= VOL_SPIKE_MULT   # volume >= 3x regular
            oi_building = oi_chg > 0 and prev_oi > 0 and oi_pct >= 3
            if is_flat and huge_vol and oi_building:
                # Direction from order-book pressure / slight price bias
                if bq > sq*1.1 or price_chg > 0:
                    acc_dist, acc_emoji = "ACCUMULATION", "🟢🔇"   # silent buying
                elif sq > bq*1.1 or price_chg < 0:
                    acc_dist, acc_emoji = "DISTRIBUTION", "🔴🔇"   # silent selling
                else:
                    acc_dist, acc_emoji = "ABSORPTION", "🟡🔇"     # unclear side
                is_unusual = True
                flags.append(f"{acc_emoji} {acc_dist} (flat price + {vol_mult:.1f}× vol + OI{oi_pct:+.0f}%)")

            st.session_state["prev"][ikey] = {"vol":vol,"oi":oi,"ltp":ltp,"ltq":ltq}

            # Show every contract that has real volume (it's a LIST).
            # Skip only dead/no-volume contracts.
            # Show any contract that has ANY volume (it's a live list).
            if vol > 0 or is_unusual:
                new.append({
                    "time":ts,"category":cat,"symbol":symbol,
                    "strike":str(sk) if sk else "FUT","type":kind,
                    "expiry":exp,"ltp":ltp,"vol_jump":vol_jump,
                    "total_vol":vol,"avg_vol":int(avg),"vol_mult":round(vol_mult,1),
                    "value_cr":round(value_cr,2),"jump_cr":round(jump_cr,2),
                    "ltq":ltq,"pressure":pressure,
                    "side":side,"side_emoji":side_emoji,
                    "acc_dist":acc_dist,"acc_emoji":acc_emoji,
                    "oi":oi,"oi_chg":oi_chg,"oi_chg_pct":round(oi_pct,1),
                    "price_chg":round(price_chg,2),
                    "activity":label,"emoji":emoji,"bias":bias,
                    "is_unusual":is_unusual,
                    "trend":f"{emoji} {label}",
                    "underlying":ltp,"reasons":" · ".join(flags) if flags else "—",
                })
            del q
    # Sort the list by volume (highest traded first)
    new.sort(key=lambda b: b["total_vol"], reverse=True)
    return new


# ── Market hours ───────────────────────────────────────────────────────────────
now=datetime.now(IST); wd=now.weekday()
nse_l=(now.replace(hour=9,minute=15,second=0,microsecond=0)<=now<=
       now.replace(hour=15,minute=30,second=0,microsecond=0)) and wd<5
mcx_l=((wd<5) and now.replace(hour=9,minute=0,second=0,microsecond=0)<=now<=
       now.replace(hour=23,minute=30,second=0,microsecond=0)) or \
      ((wd==5) and now.replace(hour=9,minute=0,second=0,microsecond=0)<=now<=
       now.replace(hour=14,minute=0,second=0,microsecond=0))

# ── Header ─────────────────────────────────────────────────────────────────────
st.markdown("## ⚡ SNY Block Order Detector")
st.caption("Institutional block scanner — Index & Commodities")

c1,c2,c3,c4=st.columns(4)
with c1:
    if auth_err == "connecting": st.warning("⏳ Connecting...")
    elif auth_err:               st.error("🔴 Auth Failed")
    elif token_map:              st.success("🟢 Ready")
    else:                        st.warning("⏳ Connecting...")
with c2: st.metric("NSE","🟢 OPEN" if nse_l else "🔴 CLOSED")
with c3: st.metric("MCX","🟢 OPEN" if mcx_l else "🔴 CLOSED")
with c4: st.metric("IST",now.strftime("%H:%M:%S"))

with st.expander("🔧 Diagnostic", expanded=False):
    # App is private (only invited viewers), so diagnostic shows directly.
    for k in ["KOTAK_CONSUMER_KEY","KOTAK_MOBILE","KOTAK_UCC","KOTAK_MPIN","KOTAK_TOTP_SECRET"]:
        v=os.environ.get(k)
        if v: st.success(f"✅ {k} present")
        else: st.error(f"❌ {k} MISSING")
    if auth_err: st.error(f"Auth: {auth_err}")
    if token_map:
        st.code(f"Tokens loaded: {sum(len(v) for v in token_map.values())} high-volume contracts across {len(token_map)} categories")
        for cat,entries in token_map.items():
            st.code(f"  {cat}: top {len(entries)} by volume")
            for e in entries[:8]:
                tok=str(e["tok"]); q=sdk_quotes.get(tok,{})
                v=_vol(q); ltp=_ltp(q)
                lbl=f"{e['sym']}" if e['type']=="FUT" else f"{e['symbol']} {e['strike']} {e['type']}"
                st.code(f"    {lbl} | vol={v:,} | ₹{ltp:,}")
        st.code(f"SDK quotes received: {len(sdk_quotes)}")
        if sdk_diag.get("stderr"):
            st.markdown("**Auth helper log:**")
            st.code(sdk_diag["stderr"])
        # (live quote test now shown inline above per category)

st.markdown("---")

# ── Live data section — auto-reruns every 60s WITHOUT full page reload ─────────
@st.cache_resource
def _quote_holder():
    return {"quotes":{}, "thread":None, "ts":0}

def _refresh_quotes_bg(holder, toks):
    try:
        proc = subprocess.run(
            [sys.executable, "quote_helper.py"],
            input=json.dumps(toks), capture_output=True, text=True,
            timeout=120, env={**os.environ, "PYTHONUNBUFFERED":"1"},
        )
        if proc.returncode==0 and proc.stdout.strip():
            data=json.loads(proc.stdout.strip())
            if "quotes" in data and data["quotes"]:
                holder["quotes"]=data["quotes"]
                holder["ts"]=time.time()
    except Exception:
        pass
    gc.collect()

@st.fragment(run_every=60 if (nse_l or mcx_l) else None)
def live_section():
    global sdk_quotes, token_map
    try:
        _s,_tm,_q,_d,_e = get_auth()
        if _tm: token_map = _tm
        if _q and not _quote_holder()["quotes"]: sdk_quotes = _q
    except: pass

    # ── Refresh quotes in background ──────────────────────────────────────────
    if token_map and (nse_l or mcx_l):
        toks=[]
        for cat,entries in token_map.items():
            for e in entries:
                if e.get("tok"): toks.append({"tok":e["tok"],"seg":e["seg"]})
        holder=_quote_holder()
        if holder["thread"] is None or not holder["thread"].is_alive():
            import threading
            holder["thread"]=threading.Thread(target=_refresh_quotes_bg,args=(holder,toks),daemon=True)
            holder["thread"].start()
        if holder["quotes"]:
            sdk_quotes = holder["quotes"]

    # ── Detect unusual activity & store per-strike ────────────────────────────
    strike_log = _STORE.setdefault("strike_log", {})

    if token_map and (nse_l or mcx_l):
        snapshot = detect_blocks()
        _STORE["snapshot"] = snapshot
        unusual = [b for b in snapshot if b.get("is_unusual")]
        for b in unusual:
            skey = f"{b['symbol']}|{b['type']}|{b['strike']}"
            slog = strike_log.setdefault(skey, [])
            slog.insert(0, b)
            del slog[30:]
            # Send to Telegram
            send_telegram(b)

        qage = int(time.time()-_quote_holder()["ts"]) if _quote_holder()["ts"] else -1
        st.caption(f"Monitoring {sum(len(v) for v in token_map.values())} contracts | "
                   f"quotes {qage}s old | {datetime.now(IST).strftime('%H:%M:%S')}")
    elif not (nse_l or mcx_l):
        st.info("Markets closed — NSE 9:15–15:30 · MCX 9:00–23:30 IST")

    # ══ TWO TABS: NSE / Commodities ══════════════════════════════════════════
    def _render_tab(category):
        symbols = CATEGORIES.get(category, [])

        # Acc/Dist alerts on top (silent institutional absorption)
        acc_hits = [b for b in _STORE.get("snapshot",[])
                    if b.get("acc_dist") and b.get("category")==category]
        if acc_hits:
            st.markdown("##### 🔇 Accumulation / Distribution")
            for b in acc_hits:
                contract = (f"{b['symbol']} FUT" if b['type']=="FUT"
                            else f"{b['symbol']} {b['strike']} {b['type']}")
                line = (f"{b.get('acc_emoji','')} **{b.get('acc_dist','')}** — {contract} "
                        f"· ₹{b['ltp']:,} (flat Δ{b.get('price_chg',0):+.1f}) "
                        f"· Vol {b.get('vol_mult',0):.1f}× · OI {b['oi_chg_pct']:+.0f}%")
                if "ACCU" in b.get("acc_dist",""):   st.success(line)
                elif "DIST" in b.get("acc_dist",""): st.error(line)
                else:                                 st.warning(line)
            st.markdown("")

        # Per-strike tables (each strike = its own table, new rows on top)
        any_shown = False
        for symbol in symbols:
            sym_keys = sorted(
                [k for k in strike_log if k.startswith(f"{symbol}|")],
                key=lambda k: strike_log[k][0]["total_vol"] if strike_log[k] else 0,
                reverse=True)
            for skey in sym_keys:
                rows = strike_log[skey]
                if not rows: continue
                any_shown = True
                parts = skey.split("|")
                title = f"{parts[0]} FUT" if parts[1]=="FUT" else f"{parts[0]} {parts[2]} {parts[1]}"
                latest = rows[0]
                st.markdown(f"**{title}** {latest.get('emoji','')} · ₹{latest['ltp']:,} · {len(rows)} events")
                st.dataframe(pd.DataFrame([{
                    "Time": r["time"],
                    "Volume": f"{r['total_vol']:,}",
                    "Vol Δ": f"+{r['vol_jump']:,}" if r['vol_jump']>0 else f"{r['vol_jump']:,}",
                    "×Avg": (f"{r.get('vol_mult',0):.1f}×" if r.get('vol_mult',0)>0 else ""),
                    "Buy/Sell": f"{r.get('side_emoji','')} {r.get('side','')}",
                    "LTP": f"₹{r['ltp']:,}",
                    "OI Δ%": f"{r['oi_chg_pct']:+.0f}%",
                    "Activity": f"{r.get('emoji','')} {r.get('activity','')}",
                    "Acc/Dist": (f"{r.get('acc_emoji','')} {r.get('acc_dist','')}"
                                 if r.get('acc_dist') else ""),
                    "Signal": r.get("reasons",""),
                } for r in rows]),width="stretch",hide_index=True)
                st.markdown("")

        if not any_shown:
            if not (nse_l or mcx_l) or not token_map:
                st.caption("Waiting for market data...")
            else:
                st.caption("No unusual activity yet — only very large/unusual volume events appear here.")

    tab_nse, tab_stk, tab_mcx = st.tabs(["📈 NSE Index","📊 Stocks","🛢️ Commodities"])
    with tab_nse:  _render_tab("Index")
    with tab_stk:  _render_tab("Stock")
    with tab_mcx:  _render_tab("Commodity")

live_section()
st.caption("Auto-updates every 60s")

# ══ CUSTOM SYMBOL ANALYZER ════════════════════════════════════════════════════
st.markdown("---")
st.markdown("### 🔍 Analyze Any Symbol")

c1, c2, c3 = st.columns([3, 2, 1])
with c1:
    analyze_sym = st.text_input("Symbol", placeholder="e.g. TATAMOTORS, GOLD, BAJFINANCE", key="analyze_sym")
with c2:
    analyze_exch = st.selectbox("Exchange", ["nse_fo", "mcx_fo"], format_func=lambda x: "NSE F&O" if x=="nse_fo" else "MCX", key="analyze_exch")
with c3:
    st.markdown(""); st.markdown("")  # align button
    analyze_btn = st.button("Analyze", type="primary", key="analyze_btn")

if analyze_btn and analyze_sym:
    with st.spinner(f"Fetching {analyze_sym.upper()} data... (login + quotes, ~60s)"):
        try:
            proc = subprocess.run(
                [sys.executable, "analyze_helper.py"],
                input=json.dumps({"symbol": analyze_sym, "exchange": analyze_exch}),
                capture_output=True, text=True, timeout=120,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            if proc.returncode == 0 and proc.stdout.strip():
                data = json.loads(proc.stdout.strip())
                if "error" in data:
                    st.error(f"⚠️ {data['error']}")
                else:
                    st.success(f"**{data['symbol']}** on {'NSE' if 'nse' in data['exchange'] else 'MCX'} "
                               f"· Underlying ₹{data['underlying']:,.1f} "
                               f"· {data['total_contracts']} contracts found · {data['analyzed']} analyzed")

                    results = data.get("results", [])
                    if results:
                        st.dataframe(pd.DataFrame([{
                            "Contract": r["contract"],
                            "Type": r["type"],
                            "Strike": str(r["strike"]) if r["strike"] and r["strike"] not in ("-", 0, "0") else "—",
                            "LTP": f"₹{r['ltp']:,}",
                            "Volume": f"{r['volume']:,}",
                            "OI": f"{r['oi']:,}",
                            "Change": f"{r['change']:+.1f} ({r['pct_change']:+.1f}%)",
                            "LTQ": f"{r['ltq']:,}",
                            "Buy Qty": f"{r['buy_qty']:,}",
                            "Sell Qty": f"{r['sell_qty']:,}",
                            "Flow": r["side"],
                        } for r in results]), width="stretch", hide_index=True, height=500)

                        # Quick analysis summary
                        top_vol = results[0]
                        total_buy = sum(r["buy_qty"] for r in results)
                        total_sell = sum(r["sell_qty"] for r in results)
                        max_oi = max(results, key=lambda r: r["oi"])

                        st.markdown("**Quick Analysis:**")
                        overall = "🟢 BUY-dominated" if total_buy > total_sell*1.2 else "🔴 SELL-dominated" if total_sell > total_buy*1.2 else "⚪ Balanced"
                        st.markdown(f"- Overall flow: **{overall}** (Buy {total_buy:,} vs Sell {total_sell:,})")
                        st.markdown(f"- Highest volume: **{top_vol['contract']}** ({top_vol['volume']:,} contracts)")
                        st.markdown(f"- Highest OI: **{max_oi['contract']}** ({max_oi['oi']:,})")
                    else:
                        st.warning("No quoted contracts found. The symbol might not have active F&O trading.")
            else:
                st.error(f"Analysis failed: {proc.stderr[-200:]}")
        except subprocess.TimeoutExpired:
            st.error("Analysis timed out (120s). Try again or use a simpler symbol.")
        except Exception as e:
            st.error(f"Error: {e}")
elif analyze_btn:
    st.warning("Enter a symbol to analyze.")
