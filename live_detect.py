"""
live_detect.py — headless intraday detection daemon.
Long-running process: loops every 60s during market hours, fetches quotes,
runs detect_core.run_detection, sends Telegram alerts. Independent of the
Streamlit app — runs unattended for weeks. Managed by sny-detect.service.
"""
import os, sys, json, time, subprocess, datetime
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import detect_core
from detect_core import IST, _vol, _ltp

POLL_SECONDS = 60
TG_COOLDOWN  = 300  # per-contract alert cooldown (secs) — matches app's 5-min

def _load_dotenv():
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(BASE, ".env"))
    except Exception:
        pass

def _markets_open():
    now = datetime.datetime.now(IST); wd = now.weekday()
    nse = wd < 5 and now.replace(hour=9,minute=15,second=0,microsecond=0) <= now <= now.replace(hour=15,minute=30,second=0,microsecond=0)
    mcx = (wd < 5 and now.replace(hour=9,minute=0,second=0,microsecond=0) <= now <= now.replace(hour=23,minute=30,second=0,microsecond=0)) or \
          (wd == 5 and now.replace(hour=9,minute=0,second=0,microsecond=0) <= now <= now.replace(hour=14,minute=0,second=0,microsecond=0))
    return nse, mcx

def _ensure_token_fresh():
    """Self-refresh if within 20 min of expiry (daemon runs unattended)."""
    try:
        with open(os.path.join(BASE, ".token_expiry")) as f:
            exp = datetime.datetime.fromisoformat(f.read().strip()[:19])
        mins = (exp - datetime.datetime.now()).total_seconds() / 60
        if mins < 20:
            subprocess.run([sys.executable, "refresh_token.py"], cwd=BASE, timeout=90)
    except Exception as e:
        print(f"[detect] token check err: {e}", file=sys.stderr)

def _tg_send(token, chat, msg):
    import requests
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": msg, "parse_mode": "Markdown"}, timeout=8)
    except Exception as e:
        print(f"[detect] tg err: {e}", file=sys.stderr)

def _fmt_alert(b):
    if b["type"] == "FUT":
        label = f"{b['symbol']} FUT"
    else:
        label = f"{b['symbol']} {b['strike']} {b['type']}"
    lines = [
        f"{b.get('emoji','')} *{b.get('activity','')}*",
        "━━━━━━━━━━━━━━━",
        f"📌 {label}",
        f"💰 ₹{b['ltp']:,}  (Δ{b.get('price_chg',0):+.1f})",
        f"📊 Vol: {b['total_vol']:,}  ({b.get('vol_mult',0):.1f}× avg)",
        f"📈 OI: {b['oi_chg_pct']:+.0f}%",
        f"{b.get('side_emoji','')} Side: {b.get('side','')}",
    ]
    if b.get("acc_dist"):
        lines.append(f"🔔 {b.get('acc_emoji','')} {b['acc_dist']}")
    # Show candle-spike multiples when this fired on the 5m/15m candle rule —
    # explains a low tick ×avg (the alert triggered on candle volume, not tick).
    cs5, cs15 = b.get("cs_5m", 0), b.get("cs_15m", 0)
    if cs5 or cs15:
        parts = []
        if cs5:  parts.append(f"5m {cs5:.1f}×")
        if cs15: parts.append(f"15m {cs15:.1f}×")
        lines.append("📊 Candle: " + " · ".join(parts) + " vs prev")
    lines += ["━━━━━━━━━━━━━━━", f"🕐 {b['time']} IST"]
    return "\n".join(lines)

def _get_token_map():
    out = subprocess.run([sys.executable, "auth_helper.py"], capture_output=True,
                         text=True, cwd=BASE, timeout=180)
    try:
        return json.loads(out.stdout).get("token_map", {})
    except Exception:
        print("[detect] auth parse failed", file=sys.stderr)
        return {}

def _get_quotes(token_map):
    toks = []
    for cat, entries in token_map.items():
        for e in entries:
            if e.get("tok"):
                toks.append({"tok": e["tok"], "seg": e["seg"]})
    qp = subprocess.run([sys.executable, "quote_helper.py"], input=json.dumps(toks),
                        capture_output=True, text=True, cwd=BASE, timeout=90)
    try:
        return json.loads(qp.stdout).get("quotes", {})
    except Exception:
        return {}

def main():
    _load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat  = os.environ.get("TELEGRAM_CHAT_ID", "")
    state = {"prev": defaultdict(dict), "volhist": defaultdict(list), "candle_vol": {}}
    crypto_prev, crypto_hist, crypto_sent = {}, {}, {}
    tg_sent = {}  # {skey: last_sent_ts}
    tm = {}
    tm_refreshed = 0
    print("[detect] daemon started", file=sys.stderr)

    while True:
        try:
            nse, mcx = _markets_open()
            _ensure_token_fresh()

            # ── NSE / MCX (only during market hours) ──
            if nse or mcx:
                if not tm or (time.time() - tm_refreshed) > 3600:
                    tm = _get_token_map()
                    tm_refreshed = time.time()
                quotes = _get_quotes(tm)
                if quotes:
                    snap = detect_core.run_detection(tm, quotes, state)
                    now_ts = time.time()
                    for b in snap:
                        if not b.get("is_unusual"):
                            continue
                        skey = f"{b['symbol']}|{b['type']}|{b['strike']}"
                        if now_ts - tg_sent.get(skey, 0) < TG_COOLDOWN:
                            continue
                        tg_sent[skey] = now_ts
                        if token and chat:
                            _tg_send(token, chat, _fmt_alert(b))
                else:
                    print("[detect] no quotes this cycle", file=sys.stderr)

            # ── crypto (OKX, 24/7) ──
            try:
                from crypto_helper import detect_spikes
                cresults, crypto_prev, crypto_hist = detect_spikes(crypto_prev, crypto_hist)
                for r in cresults:
                    csk = r["symbol"]
                    if crypto_sent.get(csk) == r.get("vol"):
                        continue
                    crypto_sent[csk] = r.get("vol")
                    stype = "\u26a1" if r.get("spike_type") == "tick" else "\U0001f4ca"
                    cmsg = "\n".join([
                        f"{stype} *CRYPTO VOLUME SPIKE*",
                        "\u2501"*8,
                        f"\U0001fa99 {r['symbol']}",
                        f"\U0001f4b5 ${r['ltp']:,.4f}",
                        f"\U0001f4ca Vol: {r['vol']:,.0f}  ({r['vol_mult']:.1f}\u00d7 avg)",
                        f"\U0001f550 {r['time']} IST",
                    ])
                    if token and chat:
                        _tg_send(token, chat, cmsg)
            except Exception as ce:
                print(f"[detect] crypto err: {ce}", file=sys.stderr)
        except Exception as e:
            print(f"[detect] loop err: {e}", file=sys.stderr)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
