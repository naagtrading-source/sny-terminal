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

    totp = pyotp.TOTP(secret).now()
    dl   = DhanLogin(client_id)
    r    = dl.generate_token(pin, totp)

    token = r.get("accessToken") if isinstance(r, dict) else None
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
    print(f"[refresh] OK — token updated, expires {exp}")

if __name__ == "__main__":
    main()
