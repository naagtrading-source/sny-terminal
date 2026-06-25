"""
crypto_helper.py — Binance volume spike detector
Fetches top crypto pairs, tracks volume, flags 50x spikes.
No API key needed.
"""
import requests, json, time

SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "DOGEUSDT","ADAUSDT","AVAXUSDT","MATICUSDT","DOTUSDT",
    "LINKUSDT","UNIUSDT","ATOMUSDT","LTCUSDT","ETCUSDT",
    "FILUSDT","NEARUSDT","APTUSDT","ARBUSDT","OPUSDT"
]

VOL_SPIKE_MULT = 50.0
MIN_HISTORY    = 3

def fetch_tickers():
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr", timeout=10)
        data = r.json()
        return {d["symbol"]: d for d in data if d["symbol"] in SYMBOLS}
    except Exception as e:
        return {}

def detect_spikes(prev_state, vol_hist):
    tickers = fetch_tickers()
    results = []
    now = time.strftime("%H:%M:%S")

    for sym in SYMBOLS:
        t = tickers.get(sym)
        if not t:
            continue

        vol   = float(t.get("volume", 0))
        ltp   = float(t.get("lastPrice", 0))
        count = int(t.get("count", 0))  # number of trades

        prev_vol = prev_state.get(sym, {}).get("vol", vol)
        vol_jump = vol - prev_vol

        h = vol_hist.setdefault(sym, [])
        h.append(vol_jump)
        if len(h) > 20: h.pop(0)

        avg = sum(h) / len(h) if h else 0
        vol_mult = (vol_jump / avg) if avg > 0 else 0

        has_history = len(h) >= MIN_HISTORY

        if has_history and avg > 0 and vol_mult >= VOL_SPIKE_MULT and vol_jump > 0:
            results.append({
                "time": now,
                "symbol": sym,
                "ltp": ltp,
                "vol": vol,
                "vol_jump": round(vol_jump, 4),
                "vol_mult": round(vol_mult, 1),
                "trades": count,
            })

        prev_state[sym] = {"vol": vol, "ltp": ltp}

    return results, prev_state, vol_hist
