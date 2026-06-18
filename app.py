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
    "Index":    ["NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY"],
    "Stock":    ["RELIANCE","HDFCBANK","TCS","INFY","ICICIBANK","SBIN"],
    "Commodity":["GOLDM","SILVERM","CRUDEOIL","NATURALGAS","COPPER"],
}
LOTS = {
    "NIFTY":75,"BANKNIFTY":30,"FINNIFTY":40,"MIDCPNIFTY":75,
    "RELIANCE":250,"HDFCBANK":550,"TCS":175,"INFY":400,"ICICIBANK":700,"SBIN":1500,
    "GOLDM":10,"SILVERM":5,"CRUDEOIL":100,"NATURALGAS":1250,"COPPER":2500,
}
VOL_SPIKE_MULT = 2.0
MIN_VOL_JUMP   = 5000
LARGE_VALUE_CR = 0.5
OI_CHANGE_PCT  = 5.0

# ── AUTH via subprocess — SDK loads/runs/exits, freeing its memory ─────────────
@st.cache_resource(ttl=60, show_spinner=False)
def get_auth():
    """
    Spawns auth_helper.py as a subprocess.
    The subprocess loads neo_api_client (~400MB), logs in, extracts tokens,
    prints JSON, and EXITS — freeing its memory before our main process uses it.
    Returns (session_headers, token_map, error_msg).
    """
    try:
        env = {**os.environ, "PYTHONUNBUFFERED":"1"}
        proc = subprocess.run(
            [sys.executable, "auth_helper.py"],
            capture_output=True, text=True,
            timeout=240, env=env,
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
        return data.get("session",{}), data.get("token_map",{}), data.get("quotes",{}), data.get("diag",{}), None
    except subprocess.TimeoutExpired:
        return None, {}, {}, {}, "auth_helper timed out after 240s"
    except Exception as e:
        return None, {}, {}, {}, str(e)

session, token_map, sdk_quotes, sdk_diag, auth_err = get_auth()

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
    for k in ("last_price","ltp","last_traded_price","lastPrice","LTP","ltpc",
              "c","close","price","Ltp","last_trade_price","lastTradedPrice",
              "ltP","last_traded_pric"):
        v=q.get(k)
        if v not in (None,"",0,"0",0.0):
            f=_f(v)
            if f>0: return f
    return 0.0
def _vol(q):
    if not isinstance(q,dict): return 0
    for k in ("volume","vol","volume_traded","tradedQuantity","totalTradedVolume",
              "Volume","vol_traded_today","ttv","total_traded_volume","v"):
        v=q.get(k)
        if v not in (None,""): 
            try: return max(0,int(_f(v)))
            except: pass
    return 0
def _oi(q):
    if not isinstance(q,dict): return 0
    for k in ("open_interest","oi","openInterest","OI","OpenInterest","oiDayHigh"):
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
    bq=int(_f(q.get("total_buy_quantity",q.get("buyQty",0))or 0))
    sq=int(_f(q.get("total_sell_quantity",q.get("sellQty",0))or 0))
    if bq>0 and sq>0:
        if bq>sq*1.2: return "🟢 BUY"
        if sq>bq*1.2: return "🔴 SELL"
        return "⚪ NEUT"
    return "🟢 BULL" if opt=="CE" else "🔴 BEAR"

# ── Volume history & prev state ────────────────────────────────────────────────
if "prev"    not in st.session_state: st.session_state["prev"]    = defaultdict(dict)
if "volhist" not in st.session_state: st.session_state["volhist"] = defaultdict(list)
if "feed"    not in st.session_state: st.session_state["feed"]    = []

def vh_avg(key):
    h=st.session_state["volhist"].get(key,[])
    return sum(h[:-1])/len(h[:-1]) if len(h)>=3 else 0

# ── BLOCK DETECTION ───────────────────────────────────────────────────────────
def detect_blocks():
    ts  = datetime.now(IST).strftime("%H:%M:%S")
    new = []
    for cat, symbols in CATEGORIES.items():
        for symbol in symbols:
            lot   = LOTS.get(symbol, 100)
            items = token_map.get(symbol, [])
            if not items: continue

            # Get underlying from FUT entry
            und = 0.0
            for entry in items:
                if entry.get("type") == "FUT":
                    q = live_quote(entry["tok"], entry["seg"])
                    und = _ltp(q)
                    if und > 0: break

            if und <= 0: continue

            for entry in items:
                tok = entry.get("tok")
                if not tok: continue
                seg  = entry["seg"]
                kind = entry["type"]
                sk   = entry.get("strike")
                exp  = entry.get("expiry","")
                sym  = entry.get("sym","")

                q   = live_quote(tok, seg)
                ltp = _ltp(q); vol = _vol(q); oi = _oi(q); ltq = _ltq(q)
                if vol <= 0 and ltp <= 0: continue

                ikey = f"{symbol}|{kind}|{sk}|{exp}"

                # Update history
                h = st.session_state["volhist"][ikey]
                h.append(vol)
                if len(h) > 15: h.pop(0)

                prev     = st.session_state["prev"].get(ikey, {})
                prev_vol = prev.get("vol", vol)
                prev_oi  = prev.get("oi",  oi)
                vol_jump = vol - prev_vol
                avg      = vh_avg(ikey)
                oi_chg   = oi  - prev_oi
                oi_pct   = (oi_chg/prev_oi*100) if prev_oi > 0 else 0

                is_block = False; reasons = []
                if avg > 0 and vol_jump >= MIN_VOL_JUMP and vol_jump >= avg * VOL_SPIKE_MULT:
                    is_block = True
                    reasons.append(f"Vol+{vol_jump:,} ({vol_jump/avg:.1f}×avg)")
                value_cr = (vol_jump * ltp) / 1e7
                if value_cr >= LARGE_VALUE_CR and vol_jump >= MIN_VOL_JUMP:
                    is_block = True; reasons.append(f"₹{value_cr:.2f}Cr")
                if ltq >= lot * 50 and ltq > 0:
                    is_block = True; reasons.append(f"BigTrade {ltq:,}")
                if abs(oi_pct) >= OI_CHANGE_PCT and prev_oi > 0:
                    is_block = True
                    reasons.append(f"OI {'↑ADD' if oi_chg>0 else '↓EXIT'} {abs(oi_pct):.0f}%")

                st.session_state["prev"][ikey] = {"vol":vol,"oi":oi,"ltp":ltp}

                if is_block:
                    new.append({
                        "time":ts,"category":cat,"symbol":symbol,
                        "strike":str(sk) if sk else "FUT","type":kind,
                        "expiry":exp,"ltp":ltp,"vol_jump":vol_jump,
                        "total_vol":vol,"avg_vol":int(avg),
                        "value_cr":round(value_cr,2),"ltq":ltq,
                        "oi":oi,"oi_chg_pct":round(oi_pct,1),
                        "trend":_trend(q,kind if kind in("CE","PE") else "CE"),
                        "underlying":und,"reasons":" | ".join(reasons),
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
st.caption("Institutional block scanner — Index · Stocks · Commodities")

c1,c2,c3,c4=st.columns(4)
with c1:
    if auth_err:     st.error("🔴 Auth Failed")
    elif token_map:  st.success("🟢 Ready")
    else:            st.warning("⏳ Connecting...")
with c2: st.metric("NSE","🟢 OPEN" if nse_l else "🔴 CLOSED")
with c3: st.metric("MCX","🟢 OPEN" if mcx_l else "🔴 CLOSED")
with c4: st.metric("IST",now.strftime("%H:%M:%S"))

with st.expander("🔧 Diagnostic", expanded=bool(auth_err)):
    for k in ["KOTAK_CONSUMER_KEY","KOTAK_MOBILE","KOTAK_UCC","KOTAK_MPIN","KOTAK_TOTP_SECRET"]:
        v=os.environ.get(k)
        if v: st.success(f"✅ {k} ({len(v)} chars)")
        else: st.error(f"❌ {k} MISSING")
    if auth_err: st.error(f"Auth: {auth_err}")
    if session:
        st.markdown("**Captured session fields:**")
        for sk,sv in session.items():
            # Mask the values but show key + length so we know what we have
            st.code(f"{sk} = {sv[:25]}... ({len(sv)} chars)")
    if token_map:
        st.code(f"Tokens loaded: {sum(len(v) for v in token_map.values())} entries across {len(token_map)} symbols")
        st.code(f"SDK quotes received: {len(sdk_quotes)}")
        if sdk_diag:
            st.markdown("**Quote API diagnostic:**")
            for dk,dv in sdk_diag.items():
                st.code(f"{dk}: {dv}")
        st.markdown("**Live quote test:**")
        tested=0
        for sym,entries in token_map.items():
            if not entries or tested>=5: continue
            fut=next((e for e in entries if e.get("type")=="FUT"), entries[0])
            q=live_quote(fut["tok"])
            ltp=_ltp(q)
            st.code(f"{sym} {fut.get('type')} tok={fut['tok']} → LTP=₹{ltp} | raw={str(q)[:200]}")
            tested+=1

st.markdown("---")

# ── Live data section — auto-reruns every 60s WITHOUT full page reload ─────────
@st.fragment(run_every=65 if (nse_l or mcx_l) else None)
def live_section():
    # Re-fetch fresh quotes each rerun (get_auth cache TTL=60s → re-spawns subprocess)
    global sdk_quotes, token_map
    _sess,_tm,_q,_diag,_err = get_auth()
    if _q: sdk_quotes = _q
    if _tm: token_map = _tm
    if token_map and (nse_l or mcx_l):
        new_blocks = detect_blocks()
        if new_blocks:
            for b in reversed(new_blocks):
                st.session_state["feed"].insert(0, b)
            st.session_state["feed"] = st.session_state["feed"][:60]
        st.caption(f"🔄 Monitoring {sum(len(v) for v in token_map.values())} instruments | "
                   f"{len(st.session_state['feed'])} blocks logged | "
                   f"updated {datetime.now(IST).strftime('%H:%M:%S')}")

    st.markdown("### 📡 Live Block Feed")
    feed=st.session_state["feed"]
    if not feed:
        if not (nse_l or mcx_l):
            st.info("🌙 Markets closed. NSE 9:15–15:30 | MCX 9:00–23:30 IST")
        elif not token_map:
            st.warning("⏳ Auth in progress — will start scanning once connected.")
        else:
            st.caption("⏳ Monitoring... blocks appear here as they trigger.")
    else:
        for b in feed[:25]:
            icon="🔵" if b["type"]=="CE" else "🔴" if b["type"]=="PE" else "🟡"
            line=(f"`{b['time']}` {icon} **{b['symbol']} {b['strike']} {b['type']}**"
                  f" [{b['expiry']}] | LTP ₹{b['ltp']} | **{b['reasons']}** | {b['trend']}")
            val=b.get("value_cr",0)
            if val>=2.0:   st.error(line)
            elif val>=0.5: st.warning(line)
            else:          st.info(line)

    st.markdown("---")
    t1,t2,t3=st.tabs(["📈 Index Blocks","📊 Stock Blocks","🛢️ Commodity Blocks"])
    def tbl(cat):
        rows=[b for b in feed if b["category"]==cat]
        if not rows: st.caption(f"No {cat.lower()} blocks detected yet."); return
        st.dataframe(pd.DataFrame([{
            "Time":b["time"],"Symbol":b["symbol"],"Strike":b["strike"],
            "Type":b["type"],"Expiry":b["expiry"],"LTP":f"₹{b['ltp']}",
            "Vol Jump":f"{b['vol_jump']:,}","Total Vol":f"{b['total_vol']:,}",
            "Avg Vol":f"{b['avg_vol']:,}","Value":f"₹{b['value_cr']}Cr",
            "OI Δ%":f"{b['oi_chg_pct']:+.0f}%","Trend":b["trend"],
            "Signals":b["reasons"],
        } for b in rows]),use_container_width=True,hide_index=True)
    with t1: tbl("Index")
    with t2: tbl("Stock")
    with t3: tbl("Commodity")

live_section()
st.caption("⏱️ Live section auto-updates every 60s (no full page reload)")
