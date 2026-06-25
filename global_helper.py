import os, time, requests
from datetime import datetime
import pytz

TWELVEDATA_KEY = os.environ.get("TWELVEDATA_API_KEY", "")

SYMBOLS = {
    "USOIL":   {"symbol": "WTI/USD",  "label": "USOIL (WTI Crude)"},
    "XAUUSD":  {"symbol": "XAU/USD",  "label": "Gold (XAU/USD)"},
    "XAGUSD":  {"symbol": "XAG/USD",  "label": "Silver (XAG/USD)"},
    "XNGUSD":  {"symbol": "XNG/USD",  "label": "Nat Gas (XNG/USD)"},
    "BITCOIN": {"symbol": "BTC/USD",  "label": "Bitcoin (BTC/USD)"},
}

_cache = {}
_last_fetch = 0
REFRESH_SEC = 60

def fetch_global():
    global _last_fetch
    now = time.time()
    if now - _last_fetch < REFRESH_SEC and _cache:
        return _cache
    syms = ",".join(s["symbol"] for s in SYMBOLS.values())
    url = f"https://api.twelvedata.com/price?symbol={syms}&apikey={TWELVEDATA_KEY}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        for key, meta in SYMBOLS.items():
            sym = meta["symbol"]
            entry = data.get(sym, {})
            price = float(entry.get("price", 0)) if entry.get("price") else None
            if price:
                prev = _cache.get(key, {}).get("price")
                chg = round(price - prev, 4) if prev else 0.0
                chg_pct = round((chg / prev) * 100, 3) if prev else 0.0
                _cache[key] = {
                    "label": meta["label"],
                    "price": price,
                    "change": chg,
                    "change_pct": chg_pct,
                    "ts": datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%H:%M:%S"),
                }
        _last_fetch = now
    except Exception as ex:
        print(f"[global] fetch error: {ex}", flush=True)
    return _cache

def get_global_quotes():
    return list(fetch_global().values())
