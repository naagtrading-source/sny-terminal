"""
institutional_helper.py — fetch NSE institutional footprints.
Outputs JSON: {"bulk": [...], "block": [...], "fii_dii": [...]}
- bulk/block: nsearchives CSVs (client names!), filtered to watched symbols
  when WATCH_SYMBOLS env/arg provided, else all rows.
- fii_dii: main-site JSON endpoint (needs UA header only).
"""
import sys, json, csv, io
import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

def _fetch_csv(url):
    r = requests.get(url, headers=UA, timeout=20)
    if r.status_code != 200 or "<html" in r.text[:200].lower():
        return []
    rows = list(csv.DictReader(io.StringIO(r.text)))
    # drop the NO RECORDS placeholder
    return [row for row in rows if (row.get("Symbol") or "").strip() not in ("", "NO RECORDS")]

def _fetch_fii_dii():
    s = requests.Session(); s.headers.update({**UA, "Accept": "application/json",
        "Referer": "https://www.nseindia.com/"})
    try:
        r = s.get("https://www.nseindia.com/api/fiidiiTradeReact", timeout=20)
        if r.status_code == 200 and r.text.strip().startswith(("[", "{")):
            return r.json()
    except Exception:
        pass
    return []

def main():
    watch = set()
    raw_in = sys.stdin.read().strip()
    if raw_in:
        try:
            watch = set(json.loads(raw_in))  # optional list of symbols to filter
        except Exception:
            watch = set()
    bulk  = _fetch_csv("https://nsearchives.nseindia.com/content/equities/bulk.csv")
    block = _fetch_csv("https://nsearchives.nseindia.com/content/equities/block.csv")
    if watch:
        bulk  = [r for r in bulk  if (r.get("Symbol") or "").strip().upper() in watch]
        block = [r for r in block if (r.get("Symbol") or "").strip().upper() in watch]
    print(json.dumps({"bulk": bulk, "block": block, "fii_dii": _fetch_fii_dii()}))

if __name__ == "__main__":
    main()
