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
import os, json, re, subprocess, sys, gc
from datetime import datetime
from collections import defaultdict

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
    "Commodity":["GOLDM","SILVERM","CRUDEOIL","NATURALGAS","COPPER"],
}
LOTS = {
    "NIFTY":75,"BANKNIFTY":30,"FINNIFTY":40,"MIDCPNIFTY":75,
    "RELIANCE":250,"HDFCBANK":550,"TCS":175,"INFY":400,"ICICIBANK":700,"SBIN":1500,
    "GOLDM":10,"SILVERM":5,"CRUDEOIL":100,"NATURALGAS":1250,"COPPER":2500,
}
# Thresholds — only UNUSUAL activity (not regular liquidity)
VOL_SPIKE_MULT = 3.0       # volume jump must be > 3x this contract's own average
MIN_VOL_JUMP   = 10000     # ignore jumps under 10k contracts
LARGE_VALUE_CR = 2.0       # value of jump must exceed Rs 2 crore
OI_CHANGE_PCT  = 8.0       # OI change > 8% = real new positions
BIG_TRADE_LOTS = 50        # single trade >= 50 lots = block print

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
    return {"done":False,"result":None,"thread":None}

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
    gc.collect()

CONFIG_VERSION = "v8-volume-ranked"  # bump to force cache refresh on config change

@st.cache_resource(ttl=1200, show_spinner=False)
def get_auth(_version=CONFIG_VERSION):
    """Non-blocking auth via background thread. Returns immediately."""
    import threading
    holder=_auth_holder()
    # Start auth in background if not already running
    if holder["thread"] is None:
        holder["thread"]=threading.Thread(target=_run_auth_bg,args=(holder,),daemon=True)
        holder["thread"].start()
    # Wait up to 45s for it (under the 60s health-check limit)
    holder["thread"].join(timeout=45)
    if holder["done"] and holder["result"]:
        return holder["result"]
    # Not done yet — return "connecting" state; next rerun will pick up result
    return (None,{},{},{},"connecting")

def _OLD_get_auth():
    try:
        env = {**os.environ, "PYTHONUNBUFFERED":"1"}
        proc = subprocess.run(
            [sys.executable, "auth_helper.py"],
            capture_output=True, text=True,
            timeout=180, env=env,
        )
        if proc.returncode != 0:
            return None, {}, {}, {}, f"auth_helper exited {proc.returncode}: {proc.stderr[-300:]}"
        stdout = proc.stdout.strip()
        if not stdout:
            return None, {}, {}, {}, f"auth_helper no output. stderr: {proc.stderr[-300:]}"
        data = json.loads(stdout)
        if "error" in data:
            return None, {}, {}, {}, f"auth error: {data['error']}"
        gc.collect()
        try:
            import ctypes
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except: pass
        st.session_state["_opt_debug"]=data.get("opt_debug",{})
        return data.get("session",{}), data.get("token_map",{}), data.get("quotes",{}), data.get("diag",{}), None
    except subprocess.TimeoutExpired:
        return None, {}, {}, {}, "auth_helper timed out after 180s — too many symbols, will retry"
    except Exception as e:
        return None, {}, {}, {}, str(e)

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
    return {"prev": defaultdict(dict), "volhist": defaultdict(list), "feed": []}

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

            # 1. Volume spike vs its OWN recent average (not just high volume)
            if avg > 0 and vol_jump >= MIN_VOL_JUMP and vol_jump >= avg * VOL_SPIKE_MULT:
                is_unusual = True
                reasons.append(f"Vol {vol_jump/avg:.1f}× normal")

            # 2. Large OI jump = fresh institutional positions
            if abs(oi_pct) >= OI_CHANGE_PCT and prev_oi > 0:
                is_unusual = True
                reasons.append(f"OI {oi_pct:+.0f}%")

            # 3. Big single trade (block print)
            if ltq >= lot * BIG_TRADE_LOTS and ltq > 0:
                is_unusual = True
                reasons.append(f"Block {ltq:,}")

            # 4. Large rupee value of the jump
            value_cr = (vol_jump * ltp) / 1e7
            if value_cr >= LARGE_VALUE_CR and vol_jump >= MIN_VOL_JUMP:
                reasons.append(f"₹{value_cr:.1f}Cr")

            st.session_state["prev"][ikey] = {"vol":vol,"oi":oi,"ltp":ltp}

            # Only log if genuinely unusual AND we have OI movement to interpret
            if is_unusual:
                label, emoji, bias = interpret_activity(kind, oi_chg, price_chg)
                new.append({
                    "time":ts,"category":cat,"symbol":symbol,
                    "strike":str(sk) if sk else "FUT","type":kind,
                    "expiry":exp,"ltp":ltp,"vol_jump":vol_jump,
                    "total_vol":vol,"avg_vol":int(avg),
                    "value_cr":round(value_cr,2),"ltq":ltq,
                    "oi":oi,"oi_chg":oi_chg,"oi_chg_pct":round(oi_pct,1),
                    "price_chg":round(price_chg,2),
                    "activity":label,"emoji":emoji,"bias":bias,
                    "trend":f"{emoji} {label}",
                    "underlying":ltp,"reasons":" · ".join(reasons),
                })
            del q
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
@st.fragment(run_every=90 if (nse_l or mcx_l) else None)
def live_section():
    # Re-read cached auth (re-spawns auth_helper only when 20-min cache expires).
    # Within cache window this is instant. Quotes refresh when cache renews.
    global sdk_quotes, token_map
    try:
        _s,_tm,_q,_d,_e = get_auth()
        if _q: sdk_quotes = _q
        if _tm: token_map = _tm
    except: pass
    if token_map and (nse_l or mcx_l):
        new_blocks = detect_blocks()
        if new_blocks:
            for b in reversed(new_blocks):
                _STORE["feed"].insert(0, b)
            # Trim in-place to preserve the persistent reference
            del _STORE["feed"][60:]
        st.caption(f"🔄 Monitoring top {sum(len(v) for v in token_map.values())} high-volume contracts | "
                   f"{len(st.session_state['feed'])} blocks logged | "
                   f"updated {datetime.now(IST).strftime('%H:%M:%S')}")

    st.markdown("### 📡 Live Block Feed")
    feed=_STORE["feed"]
    if not feed:
        if not (nse_l or mcx_l):
            st.info("🌙 Markets closed. NSE 9:15–15:30 | MCX 9:00–23:30 IST")
        elif not token_map:
            st.warning("⏳ Auth in progress — will start scanning once connected.")
        else:
            st.caption("⏳ Monitoring... blocks appear here as they trigger.")
    else:
        for b in feed[:25]:
            # Clean label: "GOLDM FUT" or "NIFTY 24500 CE"
            if b["type"]=="FUT":
                label=f"{b['symbol']} FUT"
            else:
                label=f"{b['symbol']} {b['strike']} {b['type']}"
            activity = b.get("activity","")
            emoji    = b.get("emoji","")
            line=(f"`{b['time']}` {emoji} **{activity}** — {label}"
                  f" [{b['expiry']}] · ₹{b['ltp']:,} "
                  f"(Δ{b.get('price_chg',0):+.1f}) · OI {b.get('oi_chg_pct',0):+.0f}% · {b['reasons']}")
            # Color by institutional bias
            bias = b.get("bias","NEUTRAL")
            if   bias=="BULLISH": st.success(line)
            elif bias=="BEARISH": st.error(line)
            else:                 st.info(line)

    st.markdown("---")
    st.markdown("### 📊 Block Tables by Symbol")

    def _symbol_table(rows):
        """Render one symbol's blocks as a table with institutional activity."""
        st.dataframe(pd.DataFrame([{
            "Time":b["time"],
            "Strike":("—" if b["type"]=="FUT" else b["strike"]),
            "Type":b["type"],"Expiry":b["expiry"],"LTP":f"₹{b['ltp']:,}",
            "Activity":f"{b.get('emoji','')} {b.get('activity','')}",
            "Bias":b.get("bias",""),
            "Price Δ":f"{b.get('price_chg',0):+.1f}",
            "OI Δ%":f"{b['oi_chg_pct']:+.0f}%",
            "Vol Jump":f"{b['vol_jump']:,}",
            "Signals":b["reasons"],
        } for b in rows]),width="stretch",hide_index=True)

    # Category tabs, each containing a SEPARATE table per symbol
    cat_tabs=st.tabs(["📈 Index","🛢️ Commodities"])
    cat_order=[("Index",CATEGORIES["Index"]),
               ("Commodity",CATEGORIES["Commodity"])]

    for tab,(cat,symbols) in zip(cat_tabs,cat_order):
        with tab:
            any_shown=False
            for sym in symbols:
                rows=[b for b in feed if b["symbol"]==sym]
                if not rows: continue
                any_shown=True
                # Each symbol gets its own labeled expandable table
                latest=rows[0]
                st.markdown(f"#### {sym}  ·  ₹{latest['underlying']:,.1f}  ·  {len(rows)} blocks")
                _symbol_table(rows)
                st.markdown("")  # spacing
            if not any_shown:
                st.caption(f"No {cat.lower()} blocks detected yet.")

live_section()
st.caption("⏱️ Live section auto-updates every 60s (no full page reload)")
