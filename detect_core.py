"""
detect_core.py — shared intraday detection logic (single source of truth).
Imported by app.py (viewer) and live_detect.py (headless daemon).
State passed explicitly: {"prev":{}, "volhist":defaultdict(list), "candle_vol":{}}.
No Streamlit dependency.
"""
from datetime import datetime
from collections import defaultdict
import pytz

IST = pytz.timezone("Asia/Kolkata")
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

def vh_avg(volhist, key):
    h=volhist.get(key,[])
    return sum(h[:-1])/len(h[:-1]) if len(h)>=3 else 0

# ── INSTITUTIONAL ACTIVITY DETECTION ──────────────────────────────────────────

def candle_spike(cs, ikey, cat, inc, now):
    """5m/15m: flag if current candle vol >= N x previous candle.
    N = COMM_SPIKE_MULT for Commodity else VOL_SPIKE_MULT. Self-inits state."""
    inc = inc if inc > 0 else 0
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



def run_detection(token_map, quotes, state):
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

            q   = quotes.get(str(tok), {})
            ltp = _ltp(q); vol = _vol(q); oi = _oi(q); ltq = _ltq(q)
            if vol <= 0 and ltp <= 0: continue

            ikey = f"{symbol}|{kind}|{sk}|{exp}"
            h = state["volhist"][ikey]

            prev      = state["prev"].get(ikey, {})
            prev_vol  = prev.get("vol", vol)
            prev_oi   = prev.get("oi",  oi)
            prev_ltp  = prev.get("ltp", ltp)
            vol_jump  = vol - prev_vol
            # FIX: store per-tick increment (not cumulative vol) so avg is meaningful
            h.append(max(0, vol_jump))
            if len(h) > 15: h.pop(0)
            avg       = vh_avg(state['volhist'], ikey)
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

            if has_history and avg > 0 and vol_jump >= MIN_VOL_JUMP and vol_jump >= avg * (COMM_SPIKE_MULT if cat=="Commodity" else VOL_SPIKE_MULT):
                is_unusual = True
                flags.append(f"⚡ Vol {vol_jump/avg:.1f}× normal")
            if has_history and oi_sane and abs(oi_pct) >= OI_CHANGE_PCT and prev_oi > 0 and vol_jump >= MIN_VOL_JUMP and avg > 0 and vol_jump >= avg * (COMM_SPIKE_MULT if cat=="Commodity" else VOL_SPIKE_MULT):
                is_unusual = True
                flags.append(f"OI {oi_pct:+.0f}%")
            _vmult = COMM_SPIKE_MULT if cat == "Commodity" else VOL_SPIKE_MULT
            if (ltq >= lot * BIG_TRADE_LOTS and ltq > 0 and ltq != prev_ltq and vol_jump > 0
                    and has_history and avg > 0 and vol_jump >= avg * _vmult):
                is_unusual = True
                flags.append(f"Block {ltq:,}")

            # FIX: candle spike detection — 5m/15m volume vs previous candle
            _cf = candle_spike(state.setdefault('candle_vol',{}), ikey, cat, vol_jump, datetime.now(IST))
            cs_5m  = _cf.get("5m", 0.0)
            cs_15m = _cf.get("15m", 0.0)
            if cs_5m or cs_15m:
                is_unusual = True

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

            state["prev"][ikey] = {"vol":vol,"oi":oi,"ltp":ltp,"ltq":ltq}

            # Show every contract that has real volume (it's a LIST).
            # Skip only dead/no-volume contracts.
            # Show any contract that has ANY volume (it's a live list).
            if vol > 0 or is_unusual:
                new.append({
                    "time":ts,"category":cat,"symbol":symbol,
                    "strike":str(sk) if sk else "FUT","type":kind,
                    "expiry":exp,"ltp":ltp,"vol_jump":vol_jump,
                    "total_vol":vol,"avg_vol":int(avg),"vol_mult":round(vol_mult,1),
                    "cs_5m":cs_5m,"cs_15m":cs_15m,
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

