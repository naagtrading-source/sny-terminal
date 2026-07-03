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

POLL_SECONDS = 60  # 30s doubled API rate -> Dhan throttle -> bisect retry storm -> 90s timeouts every cycle
TG_COOLDOWN  = 120  # short floor only; real dedup is event-based (fresh burst since last alert)
STATE_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".detect_state.json")
SIGNAL_LOG   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals.jsonl")

def _log_signal(rec):
    """Append one signal record (JSONL) for forensic/precursor analysis."""
    try:
        rec["ts"] = datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=5, minutes=30))).isoformat(timespec="seconds")
        with open(SIGNAL_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        print(f"[detect] log err: {e}", file=sys.stderr)

def _save_state(state, tg_sent, crypto_prev, crypto_hist, crypto_sent):
    """Persist detection state so a restart resumes with baselines intact."""
    try:
        import time as _t
        cutoff = _t.time() - 86400
        blob = {
            "prev": dict(state.get("prev", {})),
            "volhist": dict(state.get("volhist", {})),
            "candle_vol": state.get("candle_vol", {}),
            "day_oi": state.get("day_oi", {}),
            "tg_sent": {k: v for k, v in tg_sent.items() if v > cutoff},
            "crypto_prev": crypto_prev, "crypto_hist": crypto_hist,
            "crypto_sent": crypto_sent,
        }
        with open(STATE_FILE, "w") as f:
            json.dump(blob, f)
    except Exception as e:
        print(f"[detect] state save err: {e}", file=sys.stderr)

def _load_state():
    try:
        with open(STATE_FILE) as f:
            b = json.load(f)
        from collections import defaultdict as _dd
        state = {"prev": _dd(dict, b.get("prev", {})),
                 "volhist": _dd(list, b.get("volhist", {})),
                 "candle_vol": b.get("candle_vol", {}),
                 "day_oi": b.get("day_oi", {})}
        return (state, b.get("tg_sent", {}), b.get("crypto_prev", {}),
                b.get("crypto_hist", {}), b.get("crypto_sent", {}))
    except Exception:
        from collections import defaultdict as _dd
        return ({"prev": _dd(dict), "volhist": _dd(list), "candle_vol": {}},
                {}, {}, {}, {})

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

def _tg_send(token, chat, msg, thread_id=None):
    import requests
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": msg, "parse_mode": "Markdown", **({"message_thread_id": int(thread_id)} if thread_id else {})}, timeout=8)
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
    _group = os.environ.get("TG_GROUP_CHAT", "")
    _topic_nse  = os.environ.get("TG_TOPIC_NSE", "")
    _topic_comm = os.environ.get("TG_TOPIC_COMMODITY", "")
    _topic_cry  = os.environ.get("TG_TOPIC_CRYPTO", "")
    # If a group is configured, send there with per-market topics; else fallback to chat.
    _dst = _group if _group else chat
    def _topic_for(cat):
        if not _group:
            return None
        if cat == "Commodity":
            return _topic_comm
        if cat == "Crypto":
            return _topic_cry
        return _topic_nse  # Index / Stock
    state, tg_sent, crypto_prev, crypto_hist, crypto_sent = _load_state()
    tg_last_vol = {}  # {skey: total_vol at last alert} — event dedup baseline
    print(f"[detect] state loaded: {len(state['prev'])} contracts", file=sys.stderr)
    _last_save = 0.0
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
                        # Event-based dedup: re-alert only on a FRESH burst.
                        # Volume traded since the last alert must itself clear
                        # the spike bar — lingering elevation doesn't re-fire.
                        _lastvol = tg_last_vol.get(skey, 0)
                        if _lastvol:
                            _fresh = b.get("total_vol", 0) - _lastvol
                            _bar = max(b.get("avg_vol", 0), 1) * (5.0 if b.get("category") == "Commodity" else 10.0)
                            _minj = 2000 if b.get("category") == "Commodity" else 50000
                            if _fresh < max(_bar, _minj):
                                continue
                        tg_sent[skey] = now_ts
                        tg_last_vol[skey] = b.get("total_vol", 0)
                        _log_signal({"src": "intraday", "sym": b.get("symbol"),
                            "strike": b.get("strike"), "otype": b.get("type"),
                            "cat": b.get("category"), "ltp": b.get("ltp"),
                            "vol_jump": b.get("vol_jump"), "total_vol": b.get("total_vol"),
                            "vol_mult": b.get("vol_mult"), "cs_5m": b.get("cs_5m"),
                            "cs_15m": b.get("cs_15m"), "oi_pct": b.get("oi_chg_pct"), "price_day_pct": b.get("price_day_pct"), "price_chg": b.get("price_chg"), "jump_cr": b.get("jump_cr"), "value_cr": b.get("value_cr"), "paired": b.get("paired", False),
                            "activity": b.get("activity"), "side": b.get("side"),
                            "acc_dist": b.get("acc_dist"), "reasons": b.get("reasons")})
                        if token and _dst:
                            _tg_send(token, _dst, _fmt_alert(b), _topic_for(b.get("category")))
                    # ── depth walls (persistent resting institutional orders) ──
                    try:
                        walls = detect_core.detect_walls(tm, quotes, state.setdefault("walls", {}))
                        for w in walls:
                            e = "🟢" if w["side"] == "buy" else "🔴"
                            wmsg = "\n".join([
                                f"🧱 *DEPTH WALL* {e}",
                                "━━━━━━━━━━━━━━━",
                                f"📌 {w['sym']}",
                                f"{e} {w['side'].upper()} wall: {w['qty']:,} ({w['lots']} lots)",
                                f"💰 @ ₹{w['price']:,}  (LTP ₹{w['ltp']:,})",
                                f"📊 {w['x_book']}× rest of book · holding {w['persist_cycles']} cycles",
                                "━━━━━━━━━━━━━━━",
                            ])
                            _log_signal({"src": "wall", "sym": w["sym"], "side": w["side"],
                                "qty": w["qty"], "lots": w["lots"], "x_book": w["x_book"],
                                "price": w["price"], "ltp": w["ltp"]})
                            if token and _dst:
                                _tg_send(token, _dst, wmsg, _topic_for(w.get("category")))
                    except Exception as we:
                        print(f"[detect] wall err: {we}", file=sys.stderr)
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
                    _log_signal({"src": "crypto", "sym": r.get("symbol"),
                        "ltp": r.get("ltp"), "vol": r.get("vol"),
                        "vol_mult": r.get("vol_mult"), "spike_type": r.get("spike_type")})
                    if token and _dst:
                        _tg_send(token, _dst, cmsg, _topic_for("Crypto"))
            except Exception as ce:
                print(f"[detect] crypto err: {ce}", file=sys.stderr)
        except Exception as e:
            print(f"[detect] loop err: {e}", file=sys.stderr)
        if time.time() - _last_save > 300:
            _save_state(state, tg_sent, crypto_prev, crypto_hist, crypto_sent)
            _last_save = time.time()
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
