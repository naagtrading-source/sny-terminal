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
import detect_core

# ── Telegram alerts ───────────────────────────────────────────────────────────
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "")

def send_telegram(block):
    """Send an unusual activity alert to Telegram.
    DISABLED: the headless daemon (live_detect.py / sny-detect.service) is now
    the sole alerter. This no-op prevents duplicate alerts when the site is open.
    The app remains a live viewer; alerting is handled by the daemon."""
    return  # daemon owns alerting
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
            f"{emoji} *{activity}*",
            f"━━━━━━━━━━━━━━━",
            f"📌 {label}",
            f"💰 ₹{block['ltp']:,}  (Δ{block.get('price_chg',0):+.1f})",
            f"📊 Vol: {block['total_vol']:,}  ({block.get('vol_mult',0):.1f}× avg)",
            f"📈 OI: {block['oi_chg_pct']:+.0f}%",
            f"{block.get('side_emoji','🔴')} Side: {block.get('side','')}",
        ]
        if acc:
            lines.append(f"🔔 {block.get('acc_emoji','')} {acc}")
        lines.append(f"━━━━━━━━━━━━━━━")
        lines.append(f"🕐 {block['time']} IST")
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
    "Index":    ["NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY"],
    "Stock":    ["RELIANCE","HDFCBANK","TCS","INFY","ICICIBANK","SBIN","AXISBANK","KOTAKBANK","LT","WIPRO","BAJFINANCE","TITAN","MARUTI","SUNPHARMA","TATAMOTORS","ADANIENT","BHARTIARTL","HINDUNILVR","ITC","HCLTECH","ULTRACEMCO","NTPC","ADANIPORTS","ONGC","POWERGRID","M&M","TATASTEEL","ASIANPAINT","COALINDIA","BAJAJFINSV","NESTLEIND","JSWSTEEL","GRASIM","HDFCLIFE","TECHM","BAJAJ-AUTO","DRREDDY","CIPLA","BEL","EICHERMOT","HINDALCO","INDUSINDBK","APOLLOHOSP","BRITANNIA","SBILIFE","HEROMOTOCO","SHRIRAMFIN","TRENT","JIOFIN","ETERNAL"],
    "Commodity":["GOLDM","SILVERM","CRUDEOIL","NATURALGAS","COPPER"],
}
LOTS = {
    "NIFTY":75,"BANKNIFTY":30,"FINNIFTY":40,"MIDCPNIFTY":75,
    "RELIANCE":250,"HDFCBANK":550,"TCS":175,"INFY":400,"ICICIBANK":700,"SBIN":1500,
    "GOLDM":10,"SILVERM":5,"CRUDEOIL":100,"NATURALGAS":1250,"COPPER":2500,
}
# Thresholds — ONLY institutional-level activity (very high bar)
VOL_SPIKE_MULT = 10.0
COMM_SPIKE_MULT = 5.0       # volume jump must be > 5x this contract's own average
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
            capture_output=True, text=True, timeout=360, env=env,
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
        session = holder["result"][0] if isinstance(holder["result"], tuple) else None
        if age < 14400 and session:  # 4 hour session cache
            return holder["result"]
        # Expired or failed — reset for fresh auth
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
    # Build token list from token_map — no cap; quote_helper batches internally
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
            timeout=120, env={**os.environ, "PYTHONUNBUFFERED":"1"},
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

ck = (session or {}).get("ck", os.environ.get("KOTAT_CONSUMER_KEY",""))

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
_TG_SENT = {}  # {skey: last_sent_timestamp}
# Mirror into session_state keys for compatibility with existing code
st.session_state["prev"]    = _STORE["prev"]
st.session_state["volhist"] = _STORE["volhist"]
st.session_state["feed"]    = _STORE["feed"]

def vh_avg(key):
    h=st.session_state["volhist"].get(key,[])
    return sum(h[:-1])/len(h[:-1]) if len(h)>=3 else 0

# ── INSTITUTIONAL ACTIVITY DETECTION ──────────────────────────────────────────
def candle_spike(ikey, cat, inc, now):
    """5m/15m: flag if current candle vol >= N x previous candle.
    N = COMM_SPIKE_MULT for Commodity else VOL_SPIKE_MULT. Self-inits state."""
    inc = inc if inc > 0 else 0
    cs = st.session_state.setdefault("candle_vol", {})
    s  = cs.setdefault(ikey, {})
    mult = COMM_SPIKE_MULT if cat == "Commodity" else VOL_SPIKE_MULT
    out = {}  # {"5m": 3.2, "15m": 0.0} — multiple of prev candle when fired, else 0
    for win, secs in (("5m", 300), ("15m", 900)):
        b = int(now.timestamp() // secs) * secs
        d = s.setdefault(win, {"b": b, "cur": 0.0, "prev": 0.0, "fired": None})
        if b != d["b"]:
            d["prev"] = d["cur"]; d["cur"] = 0.0; d["b"] = b; d["fired"] = None
        d["cur"] += inc
        prev = d["prev"]
        out[win] = 0.0
        if prev >= MIN_VOL_JUMP and d["cur"] >= prev * mult and d["fired"] != b:
            d["fired"] = b
            out[win] = round(d["cur"] / prev, 1)
    return out


def detect_blocks():
    # Delegates to the shared detect_core.run_detection so the site and the
    # headless daemon (live_detect.py) use ONE detection engine — identical
    # thresholds, logic, and Nx multiples. Any tuning in detect_core applies
    # to both automatically.
    state = {
        "prev": _STORE["prev"],
        "volhist": _STORE["volhist"],
        "candle_vol": _STORE.setdefault("candle_vol", {}),
    }
    return detect_core.run_detection(token_map, sdk_quotes, state)


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
    for k in ["DHAN_CLIENT_ID","DHAN_ACCESS_TOKEN","DHAN_TOTP_SECRET","DHAN_PIN","TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID"]:
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

def _render_daily_ad_tab():
    """Daily-timeframe accumulation/distribution vs trailing ~30 days.
    Flags futures whose today volume is the highest in >=10 days, tagged
    ACC/DIST by close-vs-open. Fetches once per day (cached), heavy (~54 calls)."""
    import datetime as _dt
    st.caption("Daily volume vs last ~30 days \u00b7 flags highest-volume day in \u226510d \u00b7 includes today's partial candle")
    today_key = _dt.date.today().isoformat()
    cache = _STORE.setdefault("daily_ad", {})
    col_a, col_b = st.columns([1, 3])
    with col_a:
        rescan = st.button("\U0001f504 Rescan", key="ad_rescan")
    have_cached = cache.get("date") == today_key and cache.get("hits") is not None
    if rescan or not have_cached:
        futs = []
        for cat in ("Index", "Stock"):
            for c in (token_map.get(cat, []) or []):
                if c.get("type") == "FUT" and c.get("tok"):
                    futs.append({"tok": c["tok"], "seg": c.get("seg", "NSE_FNO"), "sym": c.get("sym", "?")})
        if not futs:
            st.info("No futures in token map yet \u2014 wait for the scan to populate.")
            return
        with st.spinner(f"Scanning daily volume across {len(futs)} futures\u2026 (~20-40s)"):
            try:
                proc = subprocess.run(
                    [sys.executable, "daily_ad_helper.py"],
                    input=json.dumps(futs), capture_output=True, text=True,
                    cwd=os.path.dirname(__file__), timeout=120,
                )
                out = json.loads(proc.stdout) if proc.stdout.strip() else {}
                hits = out.get("hits", [])
            except Exception as e:
                st.error(f"Daily A/D scan failed: {e}")
                return
        cache["date"] = today_key
        cache["hits"] = hits
        _STORE["daily_ad"] = cache
    hits = cache.get("hits", [])
    if not hits:
        st.info("No daily accumulation/distribution signals \u2014 no future is at a \u226510-day volume high right now.")
        return
    st.dataframe(pd.DataFrame([{
        "Symbol": h["sym"],
        "Signal": f"{h['emoji']} {h['direction']}",
        "Vol Rank": f"highest in {h['rank_days']}d",
        "\u00d730d Avg": f"{h['x_avg']}\u00d7",
        "Close": f"\u20b9{h['close']:,}",
        "Day Chg": f"{h['chg_pct']:+.2f}%",
        "Today Vol": f"{h['today_vol']:,}",
    } for h in hits]), width="stretch", hide_index=True)

def _render_crypto_tab():
    from crypto_helper import detect_spikes
    crypto_prev  = _STORE.setdefault("crypto_prev", {})
    crypto_hist  = _STORE.setdefault("crypto_hist", {})
    crypto_log   = _STORE.setdefault("crypto_log", {})

    results, crypto_prev, crypto_hist = detect_spikes(crypto_prev, crypto_hist)
    _STORE["crypto_prev"] = crypto_prev
    _STORE["crypto_hist"] = crypto_hist

    for r in results:
        skey = r["symbol"]
        slog = crypto_log.setdefault(skey, [])
        if not slog or slog[0].get("vol") != r.get("vol"):
            slog.insert(0, r)
            del slog[30:]
            try:
                _stype = "⚡" if r.get("spike_type") == "tick" else "📊"
                cmsg = "\n".join([
                    f"{_stype} *CRYPTO VOLUME SPIKE*",
                    "━━━━━━━━",
                    f"🪙 {r['symbol']}",
                    f"💵 ${r['ltp']:,.4f}",
                    f"📊 Vol: {r['vol']:,.0f}  ({r['vol_mult']:.1f}× avg)",
                    f"🕐 {r['time']} IST",
                ])
                _req.post(
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                    json={"chat_id": TG_CHAT, "text": cmsg, "parse_mode": "Markdown"},
                    timeout=5,
                )
            except Exception:
                pass

    hits = {k:v for k,v in crypto_log.items() if v}
    if not hits:
        st.caption("No crypto volume spikes yet — watching for 3x normal volume.")
        return

    for sym, rows in sorted(hits.items(), key=lambda x: x[1][0]["vol_mult"], reverse=True):
        latest = rows[0]
        st.markdown(f"**{sym}** · ${latest['ltp']:,.4f} · {len(rows)} events")
        st.dataframe([{
            "Time": r["time"],
            "Vol Jump": round(r["vol_jump"],2),
            "×Avg": f"{r['vol_mult']}×" + (" ⚡" if r.get('spike_type') == 'tick' else " 📊"),
            "Price": f"${r['ltp']:,.4f}",
            "Trades": r["trades"],
        } for r in rows], use_container_width=True, hide_index=True)

_LAST_TOKEN_REFRESH = 0
def _ensure_token_fresh():
    """If the Dhan token is within 20 min of expiry, refresh it.
    Helpers re-read .env each call, so the new token flows automatically."""
    global _LAST_TOKEN_REFRESH
    import datetime, time as _t
    try:
        with open("/home/naag_qc/sny-bot/.token_expiry") as _f:
            exp_raw = _f.read().strip()
        exp = datetime.datetime.fromisoformat(exp_raw[:19])
        mins_left = (exp - datetime.datetime.now()).total_seconds() / 60
        if mins_left < 20 and (_t.time() - _LAST_TOKEN_REFRESH) > 180:
            _LAST_TOKEN_REFRESH = _t.time()
            subprocess.run([sys.executable, "refresh_token.py"],
                           cwd=os.path.dirname(__file__), timeout=90)
    except Exception:
        pass  # never let the guard crash the scan


def live_section():
    global sdk_quotes, token_map
    _ensure_token_fresh()
    try:
        _s,_tm,_q,_d,_e = get_auth()
        if _tm: token_map = _tm
        if _q and not _quote_holder()["quotes"]: sdk_quotes = _q  # fallback only
    except: pass

    # ── Refresh quotes in background ──────────────────────────────────────────
    # FIX: no [:50] cap — quote_helper batches internally; send ALL tokens
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
        # Wait for first fetch so initial render has data
        if not holder["quotes"] and holder["thread"] and holder["thread"].is_alive():
            holder["thread"].join(timeout=15)
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
            if not slog or slog[0].get("total_vol") != b.get("total_vol"):
                slog.insert(0, b)
                del slog[30:]
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
                    "×Avg": (f"{r.get('vol_mult',0):.1f}× ⚡" if r.get('vol_mult',0)>0 else ""),
                    "5m": (f"{r.get('cs_5m',0):.1f}× 📊" if r.get('cs_5m',0)>0 else ""),
                    "15m": (f"{r.get('cs_15m',0):.1f}× 📊" if r.get('cs_15m',0)>0 else ""),
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

    def _render_global():
        from global_helper import get_global_quotes
        quotes = get_global_quotes()
        if not quotes:
            st.info("Loading global markets...")
            return
        for q in quotes:
            chg = q.get("change", 0)
            chg_pct = q.get("change_pct", 0)
            color = "#00c853" if chg >= 0 else "#ff1744"
            arrow = "\u25b2" if chg >= 0 else "\u25bc"
            st.markdown(f"""<div style='background:#161b22;border-radius:8px;padding:10px 16px;margin-bottom:6px;font-family:monospace'>
<span style='color:#8b949e'>{q["label"]}</span>&nbsp;&nbsp;
<span style='color:#e6edf3;font-size:1.1em'>${q["price"]:,.4f}</span>&nbsp;&nbsp;
<span style='color:{color}'>{arrow} {chg:+.4f} ({chg_pct:+.3f}%)</span>&nbsp;&nbsp;
<span style='color:#484f58;font-size:0.8em'>{q.get("ts","")}</span>
</div>""", unsafe_allow_html=True)

    tab_nse, tab_stk, tab_mcx, tab_crypto, tab_ad = st.tabs(["\U0001f4c8 NSE Index","\U0001f4ca Stocks","\U0001f6e2 Commodities","\U0001fa99 Crypto","\U0001f4c5 Daily A/D"])
    with tab_nse: _render_tab("Index")
    with tab_stk: _render_tab("Stock")
    with tab_mcx: _render_tab("Commodity")
    with tab_crypto: _render_crypto_tab()
    with tab_ad: _render_daily_ad_tab()

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
