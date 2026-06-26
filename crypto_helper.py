"""
crypto_helper.py — CoinGecko volume spike detector
No API key needed. Works from Indian IPs.
"""
import requests, time

# CoinGecko coin IDs mapped to display symbols
COINS = {
    "bitcoin":"BTCUSDT","ethereum":"ETHUSDT","binancecoin":"BNBUSDT",
    "solana":"SOLUSDT","ripple":"XRPUSDT","dogecoin":"DOGEUSDT",
    "cardano":"ADAUSDT","tron":"TRXUSDT","avalanche-2":"AVAXUSDT",
    "chainlink":"LINKUSDT","polkadot":"DOTUSDT","litecoin":"LTCUSDT",
    "shiba-inu":"SHIBUSDT","uniswap":"UNIUSDT","cosmos":"ATOMUSDT",
    "ethereum-classic":"ETCUSDT","stellar":"XLMUSDT","bitcoin-cash":"BCHUSDT",
    "aptos":"APTUSDT","filecoin":"FILUSDT","arbitrum":"ARBUSDT",
    "near":"NEARUSDT","optimism":"OPUSDT","injective-protocol":"INJUSDT",
    "thorchain":"RUNEUSDT","fetch-ai":"FETUSDT","sui":"SUIUSDT",
    "celestia":"TIAUSDT","sei-network":"SEIUSDT","worldcoin-wld":"WLDUSDT",
    "jupiter-exchange-solana":"JUPUSDT","stacks":"STXUSDT","aave":"AAVEUSDT",
    "maker":"MKRUSDT","havven":"SNXUSDT","compound-governance-token":"COMPUSDT",
    "curve-dao-token":"CRVUSDT","lido-dao":"LDOUSDT","render-token":"RNDRUSDT",
    "the-graph":"GRTUSDT","apecoin":"APEUSDT","the-sandbox":"SANDUSDT",
    "decentraland":"MANAUSDT","axie-infinity":"AXSUSDT","vechain":"VETUSDT",
    "internet-computer":"ICPUSDT","algorand":"ALGOUSDT","elrond-erd-2":"EGLDUSDT",
    "tezos":"XTZUSDT","theta-token":"THETAUSDT","eos":"EOSUSDT",
    "hedera-hashgraph":"HBARUSDT","dydx":"DYDXUSDT","gmx":"GMXUSDT",
    "magic":"MAGICUSDT","pendle":"PENDLEUSDT","zcash":"ZECUSDT",
    "dash":"DASHUSDT","neo":"NEOUSDT","icon":"ICXUSDT",
}

VOL_SPIKE_MULT = 3.0
MIN_HISTORY    = 3

def fetch_markets():
    """Fetch market data for all coins in one API call."""
    try:
        ids = ",".join(COINS.keys())
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": ids,
                "order": "market_cap_desc",
                "per_page": 250,
                "page": 1,
                "sparkline": False,
            },
            timeout=15,
        )
        return {d["id"]: d for d in r.json()}
    except Exception as e:
        return {}

def detect_spikes(prev_state, vol_hist):
    results = []
    now = time.strftime("%H:%M:%S")

    markets = fetch_markets()
    if not markets:
        return results, prev_state, vol_hist

    for coin_id, sym in COINS.items():
        d = markets.get(coin_id)
        if not d:
            continue

        vol   = float(d.get("total_volume") or 0)   # 24hr USD volume
        ltp   = float(d.get("current_price") or 0)

        if vol <= 0 or ltp <= 0:
            continue

        h = vol_hist.setdefault(sym, [])
        h.append(vol)
        if len(h) > 30:
            h.pop(0)

        if len(h) < MIN_HISTORY:
            prev_state[sym] = {"vol": vol, "ltp": ltp}
            continue

        # Compare current volume vs average of previous readings
        avg = sum(h[:-1]) / len(h[:-1])
        if avg <= 0:
            continue

        vol_mult = vol / avg
        vol_jump = vol - avg

        if vol_mult >= VOL_SPIKE_MULT and vol_jump > 0:
            results.append({
                "time":     now,
                "symbol":   sym,
                "ltp":      ltp,
                "vol":      round(vol, 2),
                "vol_jump": round(vol_jump, 2),
                "vol_mult": round(vol_mult, 1),
                "trades":   0,
            })

        prev_state[sym] = {"vol": vol, "ltp": ltp}

    return results, prev_state, vol_hist
