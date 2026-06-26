"""
crypto_helper.py — OKX per-minute volume spike detector.
Uses 1-minute candles (real per-interval volume, not 24h cumulative).
No API key needed. Works from Indian IPs.
"""
import requests, time

SYMBOLS = [
    "BTC-USDT","ETH-USDT","SOL-USDT","XRP-USDT","DOGE-USDT",
    "BNB-USDT","ADA-USDT","AVAX-USDT","LINK-USDT","DOT-USDT",
    "TRX-USDT","LTC-USDT","BCH-USDT","UNI-USDT","ATOM-USDT",
    "ETC-USDT","XLM-USDT","APT-USDT","FIL-USDT","ARB-USDT",
    "NEAR-USDT","OP-USDT","INJ-USDT","SUI-USDT","TIA-USDT",
    "SEI-USDT","WLD-USDT","AAVE-USDT","MKR-USDT","CRV-USDT",
    "LDO-USDT","GRT-USDT","SAND-USDT","MANA-USDT","AXS-USDT",
    "ICP-USDT","ALGO-USDT","XTZ-USDT","EOS-USDT","HBAR-USDT",
    "DYDX-USDT","GMX-USDT","PENDLE-USDT","ZEC-USDT","DASH-USDT",
    "NEO-USDT","JUP-USDT","FET-USDT","RUNE-USDT","STX-USDT",
]

VOL_SPIKE_MULT = 3.0
MIN_HISTORY    = 5

def fetch_candles(inst, limit=20):
    try:
        r = requests.get(
            "https://www.okx.com/api/v5/market/candles",
            params={"instId": inst, "bar": "1m", "limit": limit},
            timeout=6,
        )
        d = r.json()
        if d.get("code") != "0":
            return []
        rows = d.get("data", [])
        out = []
        for c in reversed(rows):
            close = float(c[4])
            qvol  = float(c[7])
            confirmed = (c[8] == "1") if len(c) > 8 else True
            out.append((close, qvol, confirmed))
        return out
    except:
        return []

def detect_spikes(prev_state, vol_hist):
    results = []
    now = time.strftime("%H:%M:%S")
    for inst in SYMBOLS:
        candles = fetch_candles(inst, limit=20)
        confirmed = [c for c in candles if c[2]]
        if len(confirmed) < MIN_HISTORY + 1:
            time.sleep(0.05)
            continue
        history = confirmed[:-1]
        latest  = confirmed[-1]
        hist_vols = [c[1] for c in history]
        ltp     = latest[0]
        cur_vol = latest[1]
        avg = sum(hist_vols[-15:]) / len(hist_vols[-15:])
        if avg <= 0:
            time.sleep(0.05)
            continue
        vol_mult = cur_vol / avg
        sym = inst.replace("-", "")
        if vol_mult >= VOL_SPIKE_MULT:
            results.append({
                "time": now, "symbol": sym, "ltp": ltp,
                "vol": round(cur_vol, 2),
                "vol_jump": round(cur_vol - avg, 2),
                "vol_mult": round(vol_mult, 1), "trades": 0,
            })
        prev_state[sym] = {"vol": cur_vol, "ltp": ltp}
        time.sleep(0.05)
    return results, prev_state, vol_hist
