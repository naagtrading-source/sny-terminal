from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Sequence, Dict
import math


@dataclass
class Bar:
    o: float
    h: float
    l: float
    c: float
    vol: float
    buy_vol: float = 0.0
    sell_vol: float = 0.0


@dataclass
class Block:
    direction: int
    top: float
    bot: float
    total_vol: float
    buy_pct: float
    sell_pct: float
    score: int
    grade: str
    components: Dict[str, bool] = field(default_factory=dict)


def _ema(values, length):
    if not values:
        return float("nan")
    k = 2.0 / (length + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def _sma(values, length):
    w = values[-length:] if len(values) >= length else values
    return sum(w) / len(w) if w else float("nan")


def _atr(bars, length):
    if len(bars) < 2:
        return float("nan")
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i].h, bars[i].l, bars[i - 1].c
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return _sma(trs, length)


def _last_pivot_high(bars, plen):
    n = len(bars)
    for p in range(n - 1 - plen, plen - 1, -1):
        window = bars[p - plen: p + plen + 1]
        if bars[p].h == max(b.h for b in window):
            return bars[p].h
    return None


def _last_pivot_low(bars, plen):
    n = len(bars)
    for p in range(n - 1 - plen, plen - 1, -1):
        window = bars[p - plen: p + plen + 1]
        if bars[p].l == min(b.l for b in window):
            return bars[p].l
    return None


def _split_fallback(bar):
    if bar.buy_vol or bar.sell_vol:
        return bar
    rng = max(1e-10, bar.h - bar.l)
    share = min(1.0, max(0.0, (bar.c - bar.l) / rng))
    bar.buy_vol = bar.vol * share
    bar.sell_vol = bar.vol * (1 - share)
    return bar


def _grade(s):
    return "A+" if s >= 85 else "A" if s >= 75 else "B"


def score_block(bars, *, trend_len=200, pivot_len=8, ob_search=6, atr_len=14,
                vol_len=20, vol_mult=2.0, delta_thr=55.0, disp_thr=1.5,
                range_len=50, require_trend=True, min_score=70):
    n = len(bars)
    need = max(trend_len, range_len, pivot_len + 1) + 3
    if n < need:
        return None

    bars = [_split_fallback(b) for b in bars]
    closes = [b.c for b in bars]
    cur, prev = bars[-1], bars[-2]

    ema_t = _ema(closes, trend_len)
    v_avg = _sma([b.vol for b in bars], vol_len)
    atr = _atr(bars[-(atr_len + 2):], atr_len)
    if math.isnan(atr) or atr <= 0:
        return None

    highs = [b.h for b in bars[-range_len:]]
    lows = [b.l for b in bars[-range_len:]]
    rng_mid = (max(highs) + min(lows)) / 2

    ph = _last_pivot_high(bars, pivot_len)
    pl = _last_pivot_low(bars, pivot_len)
    bos_up = ph is not None and cur.c > ph and prev.c <= ph
    bos_dn = pl is not None and cur.c < pl and prev.c >= pl
    if not (bos_up or bos_dn):
        return None

    direction = 1 if bos_up else -1

    off = None
    for i in range(1, ob_search + 1):
        b = bars[-1 - i]
        if (direction == 1 and b.c < b.o) or (direction == -1 and b.c > b.o):
            off = i
            break
    if off is None:
        return None

    ob_bar = bars[-1 - off]
    top, bot = ob_bar.h, ob_bar.l

    seg = bars[-1 - off:]
    buy_v = sum(b.buy_vol for b in seg)
    sell_v = sum(b.sell_vol for b in seg)
    peak_v = max(b.vol for b in seg)
    total = buy_v + sell_v
    if total <= 0:
        return None
    buy_pct = buy_v / total * 100
    sell_pct = 100 - buy_pct

    aligned_delta = buy_pct if direction == 1 else sell_pct
    disp = (cur.c - bot) / atr if direction == 1 else (top - cur.c) / atr

    fvg = False
    for j in range(0, off - 1):
        a = bars[-1 - j]
        b2 = bars[-1 - (j + 2)]
        if direction == 1 and a.l > b2.h:
            fvg = True
            break
        if direction == -1 and a.h < b2.l:
            fvg = True
            break

    loc = (bot < rng_mid) if direction == 1 else (top > rng_mid)
    trend_ok = (cur.c > ema_t) if direction == 1 else (cur.c < ema_t)

    comp = {
        "trend": trend_ok,
        "rel_volume": peak_v >= v_avg * vol_mult,
        "delta": aligned_delta >= delta_thr,
        "displacement": disp >= disp_thr,
        "fvg": fvg,
        "location": loc,
    }
    score = (25 * comp["trend"] + 20 * comp["rel_volume"] + 20 * comp["delta"]
             + 15 * comp["displacement"] + 10 * comp["fvg"] + 10 * comp["location"])

    if score < min_score or (require_trend and not trend_ok):
        return None

    return Block(direction, top, bot, total, round(buy_pct, 1),
                 round(sell_pct, 1), int(score), _grade(int(score)), comp)


def telegram_line(blk, symbol):
    arrow = "\U0001F7E2 BULL" if blk.direction == 1 else "\U0001F534 BEAR"
    hit = " ".join(k for k, v in blk.components.items() if v)

    def hz(x):
        for u, d in (("M", 1e6), ("K", 1e3)):
            if abs(x) >= d:
                return f"{x/d:.2f}{u}"
        return f"{x:.0f}"

    return (f"{arrow} OB [{blk.grade} {blk.score}] {symbol}\n"
            f"zone {blk.bot:.2f}-{blk.top:.2f} | vol {hz(blk.total_vol)} "
            f"(buy {blk.buy_pct:.0f}% / sell {blk.sell_pct:.0f}%)\n"
            f"confluence: {hit}")


if __name__ == "__main__":
    bars = []
    p = 100.0
    for i in range(240):
        o = p
        c = p + 0.135
        h = c + 0.1
        l = o - 0.1
        bars.append(Bar(o, h, l, c, 100.0))
        p = c
    bars.append(Bar(p, 133.0, p - 0.2, 131.8, 100.0))
    p = 131.8
    for i in range(15):
        o = p
        c = p - 0.15
        h = o + 0.02
        l = c - 0.1
        bars.append(Bar(o, h, l, c, 100.0))
        p = c
    bars.append(Bar(p, p + 0.1, p - 1.0, p - 0.9, 90.0))
    ob_c = bars[-1].c
    bars.append(Bar(ob_c, ob_c + 1.5, ob_c + 0.2, ob_c + 1.4, 300,
                    buy_vol=255, sell_vol=45))
    imp = bars[-1]
    bars.append(Bar(imp.c, 132.6, imp.h + 0.3, 132.4, 360,
                    buy_vol=320, sell_vol=40))
    prevc = bars[-1].c
    bars.append(Bar(prevc, 134.5, prevc - 0.1, 134.0, 330,
                    buy_vol=300, sell_vol=30))

    blk = score_block(bars, trend_len=120, range_len=50, min_score=50)
    if blk:
        print(telegram_line(blk, "DEMO"))
        print("components:", blk.components)
    else:
        print("no qualifying block")
