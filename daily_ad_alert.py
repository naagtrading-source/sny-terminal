"""
daily_ad_alert.py — standalone hourly Daily A/D alert sender.
Runs the daily accumulation/distribution scan (via daily_ad_helper.py)
and sends Telegram alerts for flagged futures. Dedups per contract per
hour-slot so re-runs within the same hour don't spam. Meant to be fired
by a systemd timer hourly during market hours.
"""
import os, sys, json, subprocess, datetime

BASE = os.path.dirname(os.path.abspath(__file__))
SENT_FILE = os.path.join(BASE, ".ad_alert_sent")

def _load_dotenv():
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(BASE, ".env"))
    except Exception:
        pass

def _tg_send(token, chat, msg, thread_id=None):
    import requests
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": msg, "parse_mode": "Markdown", **({"message_thread_id": int(thread_id)} if thread_id else {})},
            timeout=8,
        )
    except Exception as e:
        print(f"[ad-alert] tg send err: {e}", file=sys.stderr)

def _load_sent():
    # returns (slot_key, set_of_syms_already_sent_this_slot)
    slot = datetime.datetime.now().strftime("%Y-%m-%d-%H")
    try:
        with open(SENT_FILE) as f:
            data = json.load(f)
        if data.get("slot") == slot:
            return slot, set(data.get("syms", []))
    except Exception:
        pass
    return slot, set()

def _save_sent(slot, syms):
    try:
        with open(SENT_FILE, "w") as f:
            json.dump({"slot": slot, "syms": sorted(syms)}, f)
    except Exception as e:
        print(f"[ad-alert] save err: {e}", file=sys.stderr)

def main():
    _load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat  = os.environ.get("TELEGRAM_CHAT_ID", "")
    _group = os.environ.get("TG_GROUP_CHAT", "")
    _topic_ad = os.environ.get("TG_TOPIC_AD", "")
    _dst = _group if _group else chat
    _thread = _topic_ad if _group else None
    if not token or not chat:
        print("[ad-alert] no telegram creds", file=sys.stderr); return
    py = sys.executable

    # 1) get FUT tokens from a fresh auth scan
    out = subprocess.run([py, "auth_helper.py"], capture_output=True, text=True,
                         cwd=BASE, timeout=180)
    try:
        tm = json.loads(out.stdout).get("token_map", {})
    except Exception:
        print("[ad-alert] auth scan parse failed", file=sys.stderr); return
    futs = []
    # Only scan NSE index/stock futures while NSE is open (9:15-15:30 IST).
    # After NSE close, alert on Commodity (MCX) only — runs till 23:30.
    _ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    _now = datetime.datetime.now(_ist)
    _nse_open = (_now.weekday() < 5
                 and _now.replace(hour=9, minute=15, second=0, microsecond=0) <= _now
                 <= _now.replace(hour=15, minute=30, second=0, microsecond=0))
    _cats = ("Index", "Stock", "Commodity") if _nse_open else ("Commodity",)
    for cat in _cats:
        for c in (tm.get(cat, []) or []):
            if c.get("type") == "FUT" and c.get("tok"):
                futs.append({"tok": c["tok"], "seg": c.get("seg", "NSE_FNO"), "sym": c.get("sym", "?")})
    if not futs:
        print("[ad-alert] no futures found", file=sys.stderr); return

    # 2) run the A/D scan (single source of truth = daily_ad_helper.py)
    proc = subprocess.run([py, "daily_ad_helper.py"], input=json.dumps(futs),
                          capture_output=True, text=True, cwd=BASE, timeout=180)
    try:
        hits = json.loads(proc.stdout).get("hits", [])
    except Exception:
        print(f"[ad-alert] helper parse failed: {proc.stdout[:200]}", file=sys.stderr); return

    if not hits:
        print("[ad-alert] no A/D signals this run", file=sys.stderr); return

    # 3) dedup per contract per hour-slot
    slot, already = _load_sent()
    ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    now_str = datetime.datetime.now(ist).strftime("%H:%M")
    new_syms = set(already)
    sent_count = 0
    for h in hits:
        sym = h["sym"]
        # Dedup by symbol+direction so a trend FLIP (e.g. ACCUMULATION -> DISTRIBUTION)
        # re-alerts within the same hour, while an unchanged trend stays suppressed.
        _key = f"{sym}|{h.get('direction','')}"
        if _key in already:
            continue
        msg = "\n".join([
            f"{h['emoji']} *DAILY {h['direction']}*",
            "━━━━━━━━━━━━━━━",
            f"📌 {sym}",
            f"📊 Vol: {h['today_vol']:,}  ({h['x_avg']}× 30d avg)",
            f"🏆 Highest volume in {h['rank_days']} days",
            f"💰 ₹{h['close']:,}  ({h['chg_pct']:+.2f}%)",
            "━━━━━━━━━━━━━━━",
            f"🕐 {now_str} IST · daily timeframe",
        ])
        _tg_send(token, _dst, msg, _thread)
        new_syms.add(_key)
        sent_count += 1

    _save_sent(slot, new_syms)
    print(f"[ad-alert] sent {sent_count} new alerts (slot {slot})", file=sys.stderr)

if __name__ == "__main__":
    main()
