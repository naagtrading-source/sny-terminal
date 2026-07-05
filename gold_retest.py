"""
gold_retest.py -- gold-block retest alerts for sny-bot (NSE Nifty-50 + MCX).

Fires a Telegram alert when:
  1. a GOLD block exists  (an A/A+ order block, score >= 75, from ob_score), AND
  2. price RETESTS it      (a later candle's range re-enters the block zone), AND
  3. a STRONG-DELTA candle CLOSES there, delta aligned with the block direction.

Delta on NSE/MCX is the close-position proxy (Dhan has no intrabar buy/sell):
  buy%  = (close - low) / (high - low) * 100
  A candle closing near its high = buy-dominant; near its low = sell-dominant.
  This proxy is weakest on choppy candles, but the retest-at-a-zone filter throws
  those out -- a decisive close AT a known level is exactly where it's trustworthy.

Timeframes: 5m and 15m. Universe: Nifty-50 (NSE) + MCX commodities.
Route NSE alerts to topic 3, MCX to topic 4.

Design for the e2-micro:
  * Detection is pure-python (imports ob_score, no pandas/numpy).
  * The detector holds gold-block state across polls; feed it closed candles.
  * Only do work when a NEW candle has closed (see mark_and_check_new_bar) so
    you are NOT re-scanning every 60s -- a 5m symbol is touched ~once per 5 min.
  * Bars can be built two ways (see bottom): seed from Dhan intraday history,
    or aggregate your existing 60s quote polls into 5m/15m with aggregate_1m().
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Sequence, List, Dict
from ob_score import Bar, score_block


# ─────────────────────────────────────────────────────────────
@dataclass
class GoldBlock:
    direction: int        # +1 bullish, -1 bearish
    top: float
    bot: float
    score: int
    grade: str
    formed_ts: int        # timestamp (or bar count) when it formed
    alerted: bool = False


@dataclass
class RetestAlert:
    symbol: str
    tf: str               # "5m" / "15m"
    direction: int
    grade: str
    score: int
    zone_top: float
    zone_bot: float
    price: float
    buy_pct: float
    vol_x: float


# ─────────────────────────────────────────────────────────────
def close_pos_delta(bar: Bar) -> float:
    """Buy% proxy from where close sits in the bar's range (0..100)."""
    rng = bar.h - bar.l
    if rng <= 0:
        return 50.0
    return max(0.0, min(100.0, (bar.c - bar.l) / rng * 100.0))


class GoldRetestDetector:
    """
    One detector instance for everything. Call update() per symbol+tf with the
    latest window of CLOSED candles (oldest -> newest). Returns any RetestAlert
    that fired on the newest candle, else None.
    """
    def __init__(self, *, min_score=75, retest_delta=65.0, retest_vol_mult=1.0,
                 zone_buffer_atr=0.15, max_blocks=6, score_kwargs=None):
        self.min_score = min_score          # gold = A/A+ (>=75)
        self.retest_delta = retest_delta    # candle must be this buy%/sell% dominant
        self.retest_vol_mult = retest_vol_mult
        self.zone_buffer_atr = zone_buffer_atr
        self.max_blocks = max_blocks
        # score_block tuning per your indicator; lighter warm-up for intraday
        self.score_kwargs = score_kwargs or dict(trend_len=120, range_len=50,
                                                  vol_mult=2.0, min_score=75)
        self._blocks: Dict[str, List[GoldBlock]] = {}
        self._last_ts: Dict[str, int] = {}

    # -- helpers --
    def _key(self, symbol, tf):
        return f"{symbol}|{tf}"

    @staticmethod
    def _avg_vol(bars, n=20):
        w = [b.vol for b in bars[-n:]]
        return sum(w) / len(w) if w else 0.0

    def mark_and_check_new_bar(self, symbol, tf, newest_ts) -> bool:
        """Return True only when newest_ts is newer than last seen for this key.
        Use this to skip work when no new candle has closed."""
        k = self._key(symbol, tf)
        prev = self._last_ts.get(k)
        if prev is not None and newest_ts <= prev:
            return False
        self._last_ts[k] = newest_ts
        return True

    # -- main --
    def update(self, symbol: str, tf: str, bars: Sequence[Bar],
               newest_ts: int) -> Optional[RetestAlert]:
        k = self._key(symbol, tf)
        blocks = self._blocks.setdefault(k, [])
        if len(bars) < 5:
            return None
        cur = bars[-1]

        # 1) register a fresh GOLD block if the newest bar just formed one
        blk = score_block(bars, **self.score_kwargs)
        if blk is not None and blk.score >= self.min_score:
            # dedup: skip if a same-direction block already overlaps this zone
            dup = any(gb.direction == blk.direction and
                      blk.bot <= gb.top and gb.bot <= blk.top for gb in blocks)
            if not dup:
                blocks.append(GoldBlock(blk.direction, blk.top, blk.bot,
                                        blk.score, blk.grade, newest_ts))
                if len(blocks) > self.max_blocks:
                    blocks.pop(0)

        # 2) evaluate retest / mitigation against existing blocks
        atr = self._atr(bars)
        buf = atr * self.zone_buffer_atr
        v_avg = self._avg_vol(bars)
        fired: Optional[RetestAlert] = None

        for gb in list(blocks):
            # mitigation: price closed clean through the far side -> drop it
            if (gb.direction == 1 and cur.c < gb.bot - buf) or \
               (gb.direction == -1 and cur.c > gb.top + buf):
                blocks.remove(gb)
                continue

            if gb.alerted:
                continue
            # don't fire on the very candle that formed the block
            if newest_ts == gb.formed_ts:
                continue

            # retest: candle range re-enters the zone
            overlaps = cur.l <= gb.top + buf and cur.h >= gb.bot - buf
            if not overlaps:
                continue

            # strong close-position delta aligned with block direction
            buy_pct = close_pos_delta(cur)
            aligned = (gb.direction == 1 and buy_pct >= self.retest_delta) or \
                      (gb.direction == -1 and buy_pct <= 100 - self.retest_delta)
            if not aligned:
                continue

            # close on the correct side of the zone (defended, not broken through)
            side_ok = (gb.direction == 1 and cur.c >= gb.bot) or \
                      (gb.direction == -1 and cur.c <= gb.top)
            if not side_ok:
                continue

            # light volume gate
            if v_avg > 0 and cur.vol < v_avg * self.retest_vol_mult:
                continue

            gb.alerted = True
            fired = RetestAlert(symbol, tf, gb.direction, gb.grade, gb.score,
                                gb.top, gb.bot, cur.c, round(buy_pct, 1),
                                round(cur.vol / v_avg, 1) if v_avg > 0 else 0.0)
            # first qualifying block wins; stop here
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


# ─────────────────────────────────────────────────────────────
def telegram_line(a: RetestAlert) -> str:
    arrow = "\U0001F7E2 LONG" if a.direction == 1 else "\U0001F534 SHORT"
    side = "buy" if a.direction == 1 else "sell"
    return (f"\u2605 GOLD RETEST  {arrow}  {a.symbol} [{a.tf}]\n"
            f"grade {a.grade} {a.score} | zone {a.zone_bot:.2f}-{a.zone_top:.2f}\n"
            f"close {a.price:.2f} | {a.buy_pct:.0f}% {side}-close | vol {a.vol_x:.1f}\u00d7")


# ─────────────────────────────────────────────────────────────
# Bar sourcing
# ─────────────────────────────────────────────────────────────
def aggregate_1m(bars_1m: Sequence[Bar], minutes: int) -> List[Bar]:
    """Aggregate 1-minute Bars (oldest->newest) into N-minute Bars.
    Groups in fixed chunks of `minutes`; drops a trailing partial group.
    vol is summed; buy/sell left 0 (close-position delta computed downstream)."""
    out: List[Bar] = []
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


# --- Dhan fetch adapter (wire to your actual intraday call) -------------------
# ob_score/gold_retest need OHLCV bars. Dhan quote_data is a snapshot, so use
# Dhan's intraday minute candles to seed history, then keep extending live.
#
# The dhanhq client exposes intraday minute data; the exact method name/signature
# can vary by version, so this is left as an adapter you point at your working
# call. It must return 1-minute bars oldest->newest.
#
# def fetch_1m(dhan, security_id, segment, count=400):
#     resp = dhan.intraday_minute_data(security_id=security_id,
#                                      exchange_segment=segment,   # "NSE_EQ" / "MCX_COMM"
#                                      instrument_type="EQUITY")   # or FUTCOM etc.
#     # resp is dict-or-string (rate-limit!) -> guard like your quote_data code:
#     if not isinstance(resp, dict):
#         return []
#     d = resp.get("data", {})
#     o, h, l, c, v = d["open"], d["high"], d["low"], d["close"], d["volume"]
#     bars = [Bar(o[i], h[i], l[i], c[i], v[i]) for i in range(len(c))]
#     return bars[-count:]
#
# Then per symbol+tf:
#   b1 = fetch_1m(dhan, sid, seg)
#   bars = aggregate_1m(b1, 5)     # or 15
#   ts   = <timestamp of bars[-1]>
#   if det.mark_and_check_new_bar(sym, tf, ts):
#       alert = det.update(sym, tf, bars, ts)
#       if alert:
#           _tg_send(token, _dst, telegram_line(alert),
#                    _topic_for("NSE") if seg.startswith("NSE") else _topic_for("Commodities"))


# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Build a 5m series that forms a BULLISH gold block, then price pulls back
    # into the zone and prints a strong-buy candle -> retest alert should fire.
    bars: List[Bar] = []
    p = 100.0
    for i in range(240):                       # rising base -> swing high 133
        o = p
        c = p + 0.135
        bars.append(Bar(o, c + 0.1, o - 0.1, c, 100.0))
        p = c
    bars.append(Bar(p, 133.0, p - 0.2, 131.8, 100.0))      # swing high
    p = 131.8
    for i in range(15):                        # strictly-lower highs (no new pivot)
        o = p
        c = p - 0.15
        bars.append(Bar(o, o + 0.02, c - 0.1, c, 100.0))
        p = c
    bars.append(Bar(p, p + 0.1, p - 1.0, p - 0.9, 90.0))   # OB down candle (zone)
    ob_c = bars[-1].c
    zone_top, zone_bot = bars[-1].h, bars[-1].l
    bars.append(Bar(ob_c, ob_c + 1.5, ob_c + 0.2, ob_c + 1.4, 300))   # impulse
    imp = bars[-1]
    bars.append(Bar(imp.c, 132.6, imp.h + 0.3, 132.4, 360))          # impulse 2
    prevc = bars[-1].c
    bars.append(Bar(prevc, 134.5, prevc - 0.1, 134.0, 330))          # BOS bar -> gold block

    # now retrace back DOWN into the zone, then a strong-buy defending candle
    dn = 134.0
    for step in (132.5, 131.0, 130.0):         # walk down toward the zone
        bars.append(Bar(dn, dn + 0.2, step - 0.1, step, 120.0))
        dn = step
    # strong bull candle that dips INTO the zone and closes near its high
    zc = (zone_top + zone_bot) / 2
    bars.append(Bar(130.0, 130.2, zone_bot - 0.1, zone_top + 0.05, 260.0))  # retest!

    det = GoldRetestDetector()
    need = 130
    fired = None
    for i in range(need, len(bars) + 1):        # feed bar-by-bar like live
        window = bars[:i]
        ts = i                                  # use index as timestamp
        if det.mark_and_check_new_bar("DEMO", "5m", ts):
            a = det.update("DEMO", "5m", window, ts)
            if a:
                fired = a
    if fired:
        print(telegram_line(fired))
    else:
        # report how many gold blocks were registered, for debugging
        blks = det._blocks.get("DEMO|5m", [])
        print("no retest alert; gold blocks registered:", len(blks))
        for b in blks:
            print(f"  dir={b.direction} zone={b.bot:.2f}-{b.top:.2f} score={b.score} alerted={b.alerted}")
