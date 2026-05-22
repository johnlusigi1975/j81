"""One-shot check: is your Deriv account on the LEGACY API or the NEW one?

Run this locally with your own Deriv API token. The token stays on your
machine — it is never printed and never leaves your computer.

  1. Create a token: app.deriv.com -> Settings -> "API token"
     (tick the "Read" and "Trade" scopes). Copy the token.
  2. Run:  DERIV_TOKEN=your_token_here .venv/bin/python check_platform.py
  3. Paste me the VERDICT line + the account list (NOT the token).

What it does: tries to authenticate against Deriv's classic/legacy
WebSocket. If that works, your account can be traded by the current J81
Bot as-is. If it fails, you're on the new platform and we build the new
OAuth2 + OTP flow.
"""

from __future__ import annotations

import asyncio
import json
import os

import websockets

TOKEN = os.environ.get("DERIV_TOKEN", "").strip()
# 1089 is Deriv's public test app_id — fine for an authorize probe.
APP_ID = os.environ.get("DERIV_APP_ID", "1089")
LEGACY_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"


async def main() -> None:
    if not TOKEN:
        print("✗ No token. Set DERIV_TOKEN first:")
        print("    DERIV_TOKEN=your_token .venv/bin/python check_platform.py")
        print("  Create one at app.deriv.com -> Settings -> API token (Read + Trade).")
        return

    print(f"Probing the LEGACY Deriv WebSocket ({LEGACY_WS}) ...\n")
    try:
        async with websockets.connect(LEGACY_WS, open_timeout=20) as ws:
            await ws.send(json.dumps({"authorize": TOKEN}))
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
    except Exception as exc:
        print(f"✗ Legacy WS connection failed: {exc!r}")
        print("\nVERDICT: legacy API not reachable — you are likely on the NEW platform.")
        print("Next: we build the new OAuth2 + OTP integration.")
        return

    if "error" in resp:
        msg = resp["error"].get("message", "unknown")
        print(f"✗ Authorize rejected: {msg}")
        print("\nVERDICT: legacy auth did not accept your token. Likely the NEW platform")
        print("(or the token lacks scopes). We build the new OAuth2 + OTP integration.")
        return

    auth = resp.get("authorize", {})
    accounts = auth.get("account_list", []) or []
    print("✓ Legacy API ACCEPTED your account.\n")
    print(f"Primary loginid : {auth.get('loginid')}")
    print(f"Accounts ({len(accounts)}):")
    for a in accounts:
        kind = "DEMO" if a.get("is_virtual") else "REAL MONEY"
        print(f"   - {a.get('loginid'):<12} {kind:<10} {a.get('currency')}")
    print()
    print("VERDICT: your account works on the LEGACY API.")
    print("=> The current J81 Bot can trade it as-is. Reply 'legacy works' and we")
    print("   proceed to test the tree on your demo account.")


if __name__ == "__main__":
    asyncio.run(main())
