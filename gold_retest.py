from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Sequence, List, Dict
from ob_score import Bar, score_block


@dataclass
class GoldBlock:
    direction: int
    top: float
    bot: float
    score: int
    grade: str
    formed_ts: int
    alerted: bool = False


@dataclass
class RetestAlert:
    symbol: str
    tf: str
    direction: int
    grade: str
    score: int
    zone_top: float
    zone_bot: float
    price: float
    buy_pct: float
    vol_x: float


def close_pos_delta(bar):
    rng = bar.h - bar.l
    if rng <= 0:
        return 50.0
    return max(0.0, min(100.0, (bar.c - bar.l) / rng * 100.0))


class GoldRetestDetector:
    def __init__(self, *, min_score=75, retest_delta=65.0, retest_vol_mult=1.0,
                 zone_buffer_atr=0.15, max_blocks=6, score_kwargs=None):
        self.min_score = min_score
        self.retest_delta = retest_delta
        self.retest_vol_mult = retest_vol_mult
        self.zone_buffer_atr = zone_buffer_atr
        self.max_blocks = max_blocks
        self.score_kwargs = score_kwargs or dict(trend_len=120, range_len=50,
                                                 vol_mult=2.0, min_score=75)
        self._blocks = {}
        self._last_ts = {}

    def _key(self, symbol, tf):
        return f"{symbol}|{tf}"

    @staticmethod
    def _avg_vol(bars, n=20):
        w = [b.vol for b in bars[-n:]]
        return sum(w) / len(w) if w else 0.0

    def mark_and_check_new_bar(self, symbol, tf, newest_ts):
        k = self._key(symbol, tf)
        prev = self._last_ts.get(k)
        if prev is not None and newest_ts <= prev:
            return False
        self._last_ts[k] = newest_ts
        return True

    def update(self, symbol, tf, bars, newest_ts):
        k = self._key(symbol, tf)
        blocks = self._blocks.setdefault(k, [])
        if len(bars) < 5:
            return None
        cur = bars[-1]

        blk = score_block(bars, **self.score_kwargs)
        if blk is not None and blk.score >= self.min_score:
            dup = any(gb.direction == blk.direction and
                      blk.bot <= gb.top and gb.bot <= blk.top for gb in blocks)
            if not dup:
                blocks.append(GoldBlock(blk.direction, blk.top, blk.bot,
                                        blk.score, blk.grade, newest_ts))
                if len(blocks) > self.max_blocks:
                    blocks.pop(0)

        atr = self._atr(bars)
        buf = atr * self.zone_buffer_atr
        v_avg = self._avg_vol(bars)
        fired = None

        for gb in list(blocks):
            if (gb.direction == 1 and cur.c < gb.bot - buf) or \
               (gb.direction == -1 and cur.c > gb.top + buf):
                blocks.remove(gb)
                continue
            if gb.alerted:
                continue
            if newest_ts == gb.formed_ts:
                continue
            entered = cur.h >= gb.bot and cur.l <= gb.top
            if not entered:
                continue
            closed_in = gb.bot <= cur.c <= gb.top
            if not closed_in:
                continue
            buy_pct = close_pos_delta(cur)
            aligned = (gb.direction == 1 and buy_pct >= self.retest_delta) or \
                      (gb.direction == -1 and buy_pct <= 100 - self.retest_delta)
            if not aligned:
                continue
            if v_avg > 0 and cur.vol < v_avg * self.retest_vol_mult:
                continue
            gb.alerted = True
            fired = RetestAlert(symbol, tf, gb.direction, gb.grade, gb.score,
                                gb.top, gb.bot, cur.c, round(buy_pct, 1),
                                round(cur.vol / v_avg, 1) if v_avg > 0 else 0.0)
            break
        return fired

    @staticmethod
    def _atr(bars, length=14):
        if len(bars) < 2:
            return max(1e-9, bars[-1].h - bars[-1].l)
        trs = []
        for i in range(1, len(bars)):
            h, l, pc = bars[i].h, bars[i].l, bars[i - 1].c
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        w = trs[-length:]
        return max(1e-9, sum(w) / len(w))


def telegram_line(a):
    arrow = "\U0001F7E2 LONG" if a.direction == 1 else "\U0001F534 SHORT"
    side = "buy" if a.direction == 1 else "sell"
    return (f"\u2605 GOLD RETEST  {arrow}  {a.symbol} [{a.tf}]\n"
            f"grade {a.grade} {a.score} | zone {a.zone_bot:.2f}-{a.zone_top:.2f}\n"
            f"close {a.price:.2f} | {a.buy_pct:.0f}% {side}-close | vol {a.vol_x:.1f}\u00d7")


def aggregate_1m(bars_1m, minutes):
    out = []
    n = len(bars_1m)
    full = (n // minutes) * minutes
    for i in range(0, full, minutes):
        chunk = bars_1m[i:i + minutes]
        out.append(Bar(
            o=chunk[0].o,
            h=max(b.h for b in chunk),
            l=min(b.l for b in chunk),
            c=chunk[-1].c,
            vol=sum(b.vol for b in chunk),
        ))
    return out


if __name__ == "__main__":
    bars = []
    p = 100.0
    for i in range(240):
        o = p
        c = p + 0.135
        bars.append(Bar(o, c + 0.1, o - 0.1, c, 100.0))
        p = c
    bars.append(Bar(p, 133.0, p - 0.2, 131.8, 100.0))
    p = 131.8
    for i in range(15):
        o = p
        c = p - 0.15
        bars.append(Bar(o, o + 0.02, c - 0.1, c, 100.0))
        p = c
    bars.append(Bar(p, p + 0.1, p - 1.0, p - 0.9, 90.0))
    ob_c = bars[-1].c
    zone_top, zone_bot = bars[-1].h, bars[-1].l
    bars.append(Bar(ob_c, ob_c + 1.5, ob_c + 0.2, ob_c + 1.4, 300))
    imp = bars[-1]
    bars.append(Bar(imp.c, 132.6, imp.h + 0.3, 132.4, 360))
    prevc = bars[-1].c
    bars.append(Bar(prevc, 134.5, prevc - 0.1, 134.0, 330))
    dn = 134.0
    for step in (132.5, 131.0, 130.0):
        bars.append(Bar(dn, dn + 0.2, step - 0.1, step, 120.0))
        dn = step
    bars.append(Bar(130.0, 130.2, zone_bot - 0.1, zone_top + 0.05, 260.0))

    det = GoldRetestDetector()
    need = 130
    fired = None
    for i in range(need, len(bars) + 1):
        window = bars[:i]
        ts = i
        if det.mark_and_check_new_bar("DEMO", "5m", ts):
            a = det.update("DEMO", "5m", window, ts)
            if a:
                fired = a
    if fired:
        print(telegram_line(fired))
    else:
        blks = det._blocks.get("DEMO|5m", [])
        print("no retest alert; gold blocks:", len(blks))


# ═══ Dhan intraday fetch + universe (appended for live use) ═══
import datetime as _dt

def fetch_1m_dhan(dhan, security_id, exchange_segment, instrument_type):
    """Return today's 1-minute Bars (oldest->newest) or [] on any failure."""
    try:
        today = _dt.date.today().isoformat()
        r = dhan.intraday_minute_data(security_id=str(security_id),
                                      exchange_segment=exchange_segment,
                                      instrument_type=instrument_type,
                                      from_date=today, to_date=today, interval=1)
        if not isinstance(r, dict):
            return [], None
        d = r.get("data") or {}
        o = d.get("open") or []
        h = d.get("high") or []
        l = d.get("low") or []
        c = d.get("close") or []
        v = d.get("volume") or []
        ts = d.get("timestamp") or []
        n = min(len(o), len(h), len(l), len(c), len(v))
        if n == 0:
            return [], None
        bars = [Bar(float(o[i]), float(h[i]), float(l[i]), float(c[i]), float(v[i]))
                for i in range(n)]
        last_ts = int(ts[n-1]) if ts else n
        return bars, last_ts
    except Exception:
        return [], None


# Nifty-50 Dhan security IDs (NSE_EQ). Verify against your scrip master if any drift.
NIFTY50 = {
    "RELIANCE": 2885, "TCS": 11536, "HDFCBANK": 1333, "ICICIBANK": 4963,
    "INFY": 1594, "HINDUNILVR": 1394, "ITC": 1660, "SBIN": 3045,
    "BHARTIARTL": 10604, "KOTAKBANK": 1922, "LT": 11483, "AXISBANK": 5900,
    "BAJFINANCE": 317, "ASIANPAINT": 236, "MARUTI": 10999, "HCLTECH": 7229,
    "SUNPHARMA": 3351, "TITAN": 3506, "ULTRACEMCO": 11532, "WIPRO": 3787,
    "NESTLEIND": 17963, "ONGC": 2475, "NTPC": 11630, "TATAMOTORS": 759782,
    "POWERGRID": 14977, "M&M": 2031, "TATASTEEL": 3499, "ADANIENT": 25,
    "JSWSTEEL": 11723, "BAJAJFINSV": 16675, "COALINDIA": 20374, "HDFCLIFE": 467,
    "TECHM": 13538, "GRASIM": 1232, "INDUSINDBK": 5258, "CIPLA": 694,
    "DRREDDY": 881, "EICHERMOT": 910, "BRITANNIA": 547, "APOLLOHOSP": 157,
    "BPCL": 526, "DIVISLAB": 10940, "HEROMOTOCO": 1348, "HINDALCO": 1363,
    "TATACONSUM": 3432, "BAJAJ-AUTO": 16669, "SBILIFE": 21808, "UPL": 11287,
    "ADANIPORTS": 15083,
}

# MCX commodity futures -- fill security IDs from your scrip master (front-month).
# Left with placeholders; set the current-month IDs before enabling MCX.
MCX = {
    # "CRUDEOIL": <id>, "NATURALGAS": <id>, "GOLD": <id>, "SILVER": <id>,
    # "COPPER": <id>, "ZINC": <id>, "ALUMINIUM": <id>,
}


# ═══ Live scan helper (day-aware fetch → 15m → detector) ═══
from collections import OrderedDict as _OD

_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
_GOLD_DET = GoldRetestDetector()          # persistent state across polls
_GOLD_LASTBAR = {}                         # {symbol: last 15m bar ts processed}

def _bars15_for(dhan, security_id, segment, instrument_type, days=25):
    """Fetch 1m history, split by trading day, aggregate each day to 15m."""
    try:
        to = _dt.date.today().isoformat()
        frm = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
        r = dhan.intraday_minute_data(security_id=str(security_id),
                                      exchange_segment=segment,
                                      instrument_type=instrument_type,
                                      from_date=frm, to_date=to, interval=1)
        if not isinstance(r, dict):
            return [], None
        d = r.get("data") or {}
        o=d.get("open") or []; h=d.get("high") or []; l=d.get("low") or []
        c=d.get("close") or []; v=d.get("volume") or []; ts=d.get("timestamp") or []
        n = min(len(o),len(h),len(l),len(c),len(v),len(ts))
        if n == 0:
            return [], None
        days_map = _OD()
        for i in range(n):
            day = _dt.datetime.fromtimestamp(ts[i], _IST).date()
            days_map.setdefault(day, []).append(
                Bar(float(o[i]),float(h[i]),float(l[i]),float(c[i]),float(v[i])))
        bars15 = []
        for _day, b1 in days_map.items():
            bars15 += aggregate_1m(b1, 15)
        last_ts = int(ts[n-1])
        return bars15, last_ts
    except Exception:
        return [], None


def scan_nse_gold(dhan, tg_send, token, dst, topic, pace=0.15):
    """Scan all Nifty-50 for gold-block retests on 15m. Fire via tg_send.
    Returns count of alerts sent this pass."""
    import time as _t
    sent = 0
    for sym, sid in NIFTY50.items():
        bars15, last_ts = _bars15_for(dhan, sid, "NSE_EQ", "EQUITY")
        _t.sleep(pace)                     # pacing: ~49*0.15s ≈ 7s spread
        if not bars15 or last_ts is None:
            continue
        # only work when a NEW 15m bar has closed for this symbol
        if _GOLD_LASTBAR.get(sym) == last_ts:
            continue
        _GOLD_LASTBAR[sym] = last_ts
        if not _GOLD_DET.mark_and_check_new_bar(sym, "15m", last_ts):
            continue
        alert = _GOLD_DET.update(sym, "15m", bars15, last_ts)
        if alert:
            tg_send(token, dst, telegram_line(alert), topic)
            sent += 1
    return sent


# ═══ Indices for gold detection (IDX_I segment, INDEX instrument) ═══
INDICES = {
    "NIFTY":     {"id": 13, "seg": "IDX_I", "inst": "INDEX", "step": 50},
    "BANKNIFTY": {"id": 25, "seg": "IDX_I", "inst": "INDEX", "step": 100},
}
