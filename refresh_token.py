"""
refresh_token.py — generates a fresh Dhan access token via TOTP,
writes it into .env (replacing the old DHAN_ACCESS_TOKEN line).
Run once daily before market open.
"""
import os, sys, re
from dotenv import load_dotenv

ENV_PATH = "/home/naag_qc/sny-bot/.env"
load_dotenv(ENV_PATH)

import pyotp
from dhanhq import DhanLogin

def main():
    client_id = os.environ["DHAN_CLIENT_ID"].strip()
    pin       = os.environ["DHAN_PIN"].strip()
    secret    = os.environ["DHAN_TOTP_SECRET"].replace(" ", "").strip()

    import time as _time
    totp_gen = pyotp.TOTP(secret)
    dl   = DhanLogin(client_id)
    r = None; token = None
    for attempt in range(3):
        code = totp_gen.now()
        r = dl.generate_token(pin, code)
        token = r.get("accessToken") if isinstance(r, dict) else None
        if token:
            break
        msg = str(r.get("message","")).lower() if isinstance(r, dict) else ""
        print(f"[refresh] attempt {attempt+1} failed: {r}", file=sys.stderr)
        if "2 minutes" in msg or "rate" in msg:
            # Dhan rate limit — retrying won't help within the window; fail fast.
            break
        if attempt < 2:
            secs_into = _time.time() % 30
            _time.sleep(31 - secs_into)

    if not token:
        print(f"[refresh] FAILED: {r}", file=sys.stderr)
        sys.exit(1)

    # Read .env, replace the DHAN_ACCESS_TOKEN line
    with open(ENV_PATH) as f:
        lines = f.readlines()

    found = False
    for i, line in enumerate(lines):
        if line.startswith("DHAN_ACCESS_TOKEN="):
            lines[i] = f"DHAN_ACCESS_TOKEN={token}\n"
            found = True
            break
    if not found:
        lines.append(f"DHAN_ACCESS_TOKEN={token}\n")

    with open(ENV_PATH, "w") as f:
        f.writelines(lines)

    exp = r.get("expiryTime", "?")
    # Persist expiry so the running bot can self-check without an API call.
    try:
        with open("/home/naag_qc/sny-bot/.token_expiry", "w") as ef:
            ef.write(str(exp))
    except Exception as _e:
        print(f"[refresh] warn: could not write expiry file: {_e}", file=sys.stderr)
    print(f"[refresh] OK — token updated, expires {exp}")

if __name__ == "__main__":
    main()
