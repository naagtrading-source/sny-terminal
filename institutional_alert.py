"""
institutional_alert.py — daily EOD institutional footprint alerts.
Sends: (1) bulk/block deals on watched symbols + any deal >= 25cr -> Big Deals topic
       (2) FII/DII daily net flows -> FII-DII Flows topic
Run by sny-inst-alert.timer daily ~18:45 IST after NSE publishes.
Dedup: one send per calendar day via .inst_alert_sent.
"""
import os, sys, json, subprocess, datetime
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
SENT_FILE = os.path.join(BASE, ".inst_alert_sent")
MEGA_CR = 25.0  # deals >= 25cr flagged even off-watchlist

def _load_dotenv():
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(BASE, ".env"))
    except Exception:
        pass

def _tg_send(token, chat, msg, thread_id=None):
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": msg, "parse_mode": "Markdown",
                            **({"message_thread_id": int(thread_id)} if thread_id else {})},
                      timeout=10)
    except Exception as e:
        print(f"[inst] tg err: {e}", file=sys.stderr)

def _watched_symbols():
    out = subprocess.run([sys.executable, "auth_helper.py"], capture_output=True,
                         text=True, cwd=BASE, timeout=180)
    try:
        tm = json.loads(out.stdout).get("token_map", {})
    except Exception:
        return set()
    syms = set()
    for cat in ("Index", "Stock"):
        for c in tm.get(cat, []) or []:
            sym = (c.get("symbol") or c.get("sym", "").split("-")[0]).upper()
            if sym: syms.add(sym)
    return syms

def _f(v):
    try: return float(str(v).replace(",", ""))
    except Exception: return 0.0

def main():
    _load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat  = os.environ.get("TG_GROUP_CHAT") or os.environ.get("TELEGRAM_CHAT_ID", "")
    t_deals = os.environ.get("TG_TOPIC_DEALS", "")
    t_flows = os.environ.get("TG_TOPIC_FLOWS", "")
    if not token or not chat:
        print("[inst] no telegram creds", file=sys.stderr); return

    today = datetime.date.today().isoformat()
    try:
        if open(SENT_FILE).read().strip() == today:
            print("[inst] already sent today", file=sys.stderr); return
    except Exception:
        pass

    watch = _watched_symbols()
    proc = subprocess.run([sys.executable, "institutional_helper.py"],
                          input=json.dumps(sorted(watch)) if watch else "",
                          capture_output=True, text=True, cwd=BASE, timeout=120)
    # helper filters when given a watchlist; fetch unfiltered too for mega deals
    proc_all = subprocess.run([sys.executable, "institutional_helper.py"], input="",
                              capture_output=True, text=True, cwd=BASE, timeout=120)
    try:
        d_watch = json.loads(proc.stdout)
        d_all   = json.loads(proc_all.stdout)
    except Exception:
        print("[inst] helper parse failed", file=sys.stderr); return

    # ── Big Deals — clear per-deal blocks, sorted by value, auto-split ──
    deals = []
    seen = set()
    for kind in ("block", "bulk"):
        for src, is_watch in ((d_watch, True), (d_all, False)):
            for r in src.get(kind, []):
                qty = _f(r.get("Quantity Traded")); px = _f(r.get("Trade Price / Wght. Avg. Price"))
                cr = qty * px / 1e7
                if not is_watch and cr < MEGA_CR:
                    continue
                key = (kind, r.get("Symbol"), r.get("Client Name"), r.get("Buy/Sell"))
                if key in seen:
                    continue
                seen.add(key)
                deals.append({"kind": kind, "sym": r.get("Symbol", "?"),
                              "who": r.get("Client Name", "?"), "side": r.get("Buy/Sell", "?"),
                              "qty": qty, "px": px, "cr": cr, "watch": is_watch})
    n = len(deals)
    if n:
        deals.sort(key=lambda x: -x["cr"])
        ist_now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
        cur = [f"🏦 *INSTITUTIONAL DEALS — {ist_now.strftime('%d %b')}*",
               f"_{n} deals · biggest first · ⭐ = your watchlist_", ""]
        cur_len = sum(len(x) + 1 for x in cur); part = 1
        for dl in deals:
            e = "🟢 BUY" if dl["side"].upper().startswith("B") else "🔴 SELL"
            tag = "BLOCK" if dl["kind"] == "block" else "BULK"
            star = " ⭐" if dl["watch"] else ""
            block = "\n".join([
                f"{e}  *{dl['sym']}*  ·  {tag}{star}",
                f"👤 {dl['who']}",
                f"📦 {dl['qty']:,.0f} @ ₹{dl['px']:,.2f}    💰 *₹{dl['cr']:.1f}cr*",
            ])
            if cur_len + len(block) + 2 > 3500:
                _tg_send(token, chat, "\n".join(cur), t_deals)
                part += 1
                cur = [f"🏦 *INSTITUTIONAL DEALS (contd. {part})*", ""]
                cur_len = sum(len(x) + 1 for x in cur)
            cur.append(block); cur.append("")
            cur_len += len(block) + 2
        _tg_send(token, chat, "\n".join(cur), t_deals)

    # ── FII/DII Flows message ──
    fl = d_all.get("fii_dii", [])
    if fl:
        flines = ["🌊 *FII / DII DAILY FLOWS*", "━━━━━━━━━━━━━━━"]
        for f in fl:
            net = _f(f.get("netValue"))
            e = "🟢" if net >= 0 else "🔴"
            flines.append(f"{e} *{f.get('category','?')}*  net ₹{net:,.0f}cr")
            flines.append(f"   buy ₹{_f(f.get('buyValue')):,.0f}cr · sell ₹{_f(f.get('sellValue')):,.0f}cr  ({f.get('date','')})")
        flines.append("━━━━━━━━━━━━━━━")
        _tg_send(token, chat, "\n".join(flines), t_flows)

    with open(SENT_FILE, "w") as f:
        f.write(today)
    print(f"[inst] sent: {n} deals, {len(fl)} flow rows", file=sys.stderr)

if __name__ == "__main__":
    main()
