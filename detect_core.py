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
VOL_SPIKE_MULT = 50.0
COMM_SPIKE_MULT = 5.0       # volume jump must be > 5x this contract's own average
MIN_VOL_JUMP   = 50000     # NSE: ignore jumps under 50k (institutional = large)
MIN_VOL_JUMP_COMM = 2000   # MCX: commodities trade far lower volume; 50k never fires

def _min_jump(cat):
    return MIN_VOL_JUMP_COMM if cat == "Commodity" else MIN_VOL_JUMP

MIN_JUMP_CR_OPT = 10.0   # options: burst must move >= 10cr of turnover
MIN_JUMP_CR_FUT = 50.0   # futures: bigger notional, >= 50cr

def _tod_factor(now):
    """Time-of-day threshold scaling: open/close are naturally 5-10x heavier,
    so a '10x spike' at 09:20 is routine. Stricter bar in those windows."""
    hm = now.hour * 60 + now.minute
    return 1.0   # flat 50x floor all day (time-of-day scaling disabled)

def _spike_mult(cat, now):
    base = COMM_SPIKE_MULT if cat == "Commodity" else VOL_SPIKE_MULT
    return base * _tod_factor(now)
LARGE_VALUE_CR = 5.0       # value of jump must exceed Rs 5 crore
OI_CHANGE_PCT  = 15.0      # OI change > 15% = significant new institutional positions
BIG_TRADE_LOTS = 50        # single trade >= 50 lots = block print
BLOCK_MIN_CR   = 25.0      # a live block print must also be >= this many cr
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
        d = s.setdefault(win, {"b": b, "cur": 0.0, "hist": [], "fired": None})
        if b != d["b"]:
            # candle closed: push into rolling history (keep last 5)
            d.setdefault("hist", []).append(d["cur"])
            del d["hist"][:-5]
            d["cur"] = 0.0; d["b"] = b; d["fired"] = None
        d["cur"] += inc
        hist = d.get("hist", [])
        base = sum(hist) / len(hist) if hist else 0.0
        out[win] = 0.0
        # compare vs AVG of last 5 candles (not just previous one) — one quiet
        # candle no longer makes the next look like a spike
        if len(hist) >= 3 and base >= _min_jump(cat) and d["cur"] >= base * mult and d["fired"] != b:
            d["fired"] = b
            out[win] = round(d["cur"] / base, 1)
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
            # Session-cumulative OI: compare vs first OI seen today. Tick-over-tick
            # OI is ~0% in 60s; day-start delta is the real institutional signal.
            _dayoi = state.setdefault("day_oi", {})
            _dkey  = f"{ikey}|{datetime.now(IST).strftime('%Y-%m-%d')}"
            if _dkey not in _dayoi and oi > 0:
                _dayoi[_dkey] = oi
            _oi0 = _dayoi.get(_dkey, 0)
            # need a meaningful baseline: new/far strikes open with ~0 OI and
            # produce absurd percentages (+18900%). Below 500 OI, treat as n/a.
            oi_pct = ((oi - _oi0) / _oi0 * 100) if _oi0 >= 500 else 0
            price_chg = ltp - prev_ltp
            # Day-based price direction (vs open) — the real directional read.
            # tick price_chg wiggles ~0; day change confirms whether a move is real.
            _ohlc = q.get("ohlc") or {}
            _open = _f(_ohlc.get("open") or _ohlc.get("o") or 0)
            price_day_pct = ((ltp - _open) / _open * 100) if _open > 0 else 0.0

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

            if has_history and avg > 0 and vol_jump >= _min_jump(cat) and vol_jump >= avg * _spike_mult(cat, datetime.now(IST)):
                is_unusual = True
                flags.append(f"⚡ Vol {vol_jump/avg:.1f}× normal")
            if has_history and oi_sane and abs(oi_pct) >= OI_CHANGE_PCT and _oi0 > 0 and vol_jump >= _min_jump(cat) and avg > 0 and vol_jump >= avg * _spike_mult(cat, datetime.now(IST)):
                is_unusual = True
                flags.append(f"OI {oi_pct:+.0f}%")
            _vmult = _spike_mult(cat, datetime.now(IST))
            # ── LIVE BLOCK EXECUTION (independent of volume spike) ──
            # A single large print IS an institutional execution happening now.
            # Fires on its own — doesn't require the volume-spike rule.
            block_exec = False
            block_cr = (ltq * ltp) / 1e7 if ltq > 0 else 0
            if (ltq >= lot * BIG_TRADE_LOTS and ltq > 0 and ltq != prev_ltq
                    and block_cr >= BLOCK_MIN_CR):
                is_unusual = True
                block_exec = True
                flags.append(f"🔨 BLOCK {ltq:,} lots (₹{block_cr:.1f}cr)")

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
            is_flat = abs(price_day_pct) < 0.5   # flat on the DAY, not just this tick
            # acc/dist must ALSO clear the absolute volume floor + history —
            # 14x of a tiny average is noise, not institutional absorption
            huge_vol = (vol_mult >= VOL_SPIKE_MULT and has_history
                        and vol_jump >= _min_jump(cat))
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

            # ── CONFIRMATION FILTER: volume alone isn't tradeable ──
            # Options: vol>=25x AND (real OI>=10% OR strong day-move) AND direction
            #   confirms. Futures: OI% structurally weak, so vol>=40x AND a real
            #   day-move (>=0.4%) in the label's direction. Candle & acc/dist
            #   signals keep their own paths.
            if is_unusual and not (cs_5m or cs_15m) and not acc_dist:
                _dir_ok = ((bias == "BULLISH" and price_day_pct >= 0.3) or
                           (bias == "BEARISH" and price_day_pct <= -0.3))
                _min_cr = MIN_JUMP_CR_FUT if kind == "FUT" else MIN_JUMP_CR_OPT
                if kind == "FUT":
                    if not (vol_mult >= 40 and abs(price_day_pct) >= 0.4 and _dir_ok
                            and jump_cr >= _min_cr):
                        is_unusual = False
                else:
                    if not (vol_mult >= 25 and (abs(oi_pct) >= 10 or abs(price_day_pct) >= 1.0)
                            and _dir_ok and jump_cr >= _min_cr):
                        is_unusual = False
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
                    "block_exec":block_exec,"block_cr":round(block_cr,1),
                    "block_lots":int(ltq/lot) if lot else 0,
                    "side":side,"side_emoji":side_emoji,
                    "acc_dist":acc_dist,"acc_emoji":acc_emoji,
                    "oi":oi,"oi_chg":oi_chg,"oi_chg_pct":round(oi_pct,1),
                    "price_chg":round(price_chg,2),
                    "price_day_pct":round(price_day_pct,2),
                    "activity":label,"emoji":emoji,"bias":bias,
                    "is_unusual":is_unusual,
                    "trend":f"{emoji} {label}",
                    "underlying":ltp,"reasons":" · ".join(flags) if flags else "—",
                })
            del q
    # ── BOTH-LEGS CONFIRMATION ──
    # When CE and PE of the SAME symbol+strike+expiry both fire this cycle, that's
    # coordinated structure (straddle/strangle writing) — the strongest option tell.
    flagged = [b for b in new if b.get("is_unusual") and b.get("type") in ("CE", "PE")]
    bykey = {}
    for b in flagged:
        bykey.setdefault((b["symbol"], b["strike"], b["expiry"]), []).append(b)
    for grp in bykey.values():
        types = {b["type"] for b in grp}
        if "CE" in types and "PE" in types:
            for b in grp:
                b["paired"] = True
                b["reasons"] = "🎯 BOTH LEGS · " + b.get("reasons", "")
    # Sort: paired first, then by volume
    new.sort(key=lambda b: (b.get("paired", False), b["total_vol"]), reverse=True)
    return new



# ── DEPTH WALL DETECTION ──────────────────────────────────────────────────────
WALL_DOMINANCE  = 8.0   # level must be >= 8x the median of other levels
WALL_MIN_LOTS   = 25    # and at least this many lots resting
WALL_PERSIST    = 3     # must survive N consecutive cycles (~90s at 30s poll)
WALL_ZONE_PCT   = 0.3   # same-wall tolerance: price within 0.3%

def detect_walls(token_map, quotes, wall_state, now=None):
    """Scan top-5 depth for persistent one-sided resting walls.
    wall_state: dict persisted by caller. Returns list of wall alerts."""
    alerts = []
    seen = set()
    for cat, entries in token_map.items():
        if cat == "Crypto":
            continue
        for e in entries:
            tok = str(e.get("tok") or "")
            q = quotes.get(tok, {})
            depth = q.get("depth") if isinstance(q, dict) else None
            if not isinstance(depth, dict):
                continue
            sym = e.get("sym", "?"); symbol = e.get("symbol", sym)
            lot = LOTS.get(symbol, 100)
            ltp = _ltp(q)
            for side in ("buy", "sell"):
                levels = [l for l in (depth.get(side) or []) if _f(l.get("quantity")) > 0]
                if len(levels) < 3:
                    continue
                qtys = sorted(_f(l.get("quantity")) for l in levels)
                top = max(levels, key=lambda l: _f(l.get("quantity")))
                top_q = _f(top.get("quantity"))
                others = [x for x in qtys if x != top_q] or [qtys[0]]
                med = others[len(others)//2]
                if med <= 0 or top_q < med * WALL_DOMINANCE or top_q < lot * WALL_MIN_LOTS:
                    # not a wall this cycle -> decay any tracked wall on this side
                    wall_state.pop(f"{tok}|{side}", None)
                    continue
                price = _f(top.get("price"))
                key = f"{tok}|{side}"
                w = wall_state.get(key)
                if w and ltp > 0 and abs(price - w["price"]) / ltp * 100 <= WALL_ZONE_PCT:
                    w["count"] += 1; w["qty"] = top_q; w["price"] = price
                else:
                    w = {"count": 1, "qty": top_q, "price": price, "alerted": False}
                    wall_state[key] = w
                seen.add(key)
                grew = w.get("alerted") and top_q >= 2 * w.get("alert_qty", top_q)
                if (w["count"] >= WALL_PERSIST and not w.get("alerted")) or grew:
                    w["alerted"] = True; w["alert_qty"] = top_q
                    alerts.append({
                        "sym": sym, "symbol": symbol, "category": cat,
                        "side": side, "price": price, "qty": int(top_q),
                        "lots": round(top_q / lot, 1), "x_book": round(top_q / med, 1),
                        "ltp": ltp, "persist_cycles": w["count"],
                    })
    return alerts
