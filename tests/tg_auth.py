"""
Two-step Telegram auth helper for non-interactive environments.

Step 1 (send code):
    python tests/tg_auth.py

Step 2 (sign in with code):
    TELEGRAM_CODE=12345 python tests/tg_auth.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

try:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
except ImportError:
    print("pip install telethon")
    sys.exit(1)

API_ID      = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH    = os.getenv("TELEGRAM_API_HASH", "")
PHONE       = os.getenv("TELEGRAM_PHONE", "")
CODE        = os.getenv("TELEGRAM_CODE", "").strip()
TWO_FA      = os.getenv("TELEGRAM_2FA", "").strip()
SESSION     = Path(__file__).parent / "bot_test_session"
HASH_FILE   = Path(__file__).parent / ".tg_phone_hash"


async def main():
    client = TelegramClient(str(SESSION), API_ID, API_HASH)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"[OK] Already authorized as @{me.username} ({me.first_name})")
        await client.disconnect()
        return

    if not CODE:
        # Step 1: request code
        result = await client.send_code_request(PHONE)
        HASH_FILE.write_text(json.dumps({"phone_code_hash": result.phone_code_hash}))
        print(f"Code sent to {PHONE}")
        print("Now run:  TELEGRAM_CODE=<code> python tests/tg_auth.py")
        await client.disconnect()
        return

    # Step 2: sign in
    if not HASH_FILE.exists():
        print("❌ Hash file missing. Run without TELEGRAM_CODE first.")
        await client.disconnect()
        sys.exit(1)

    data = json.loads(HASH_FILE.read_text())
    phone_code_hash = data["phone_code_hash"]

    try:
        await client.sign_in(PHONE, CODE, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        if not TWO_FA:
            print("2FA required. Rerun with: TELEGRAM_CODE=<code> TELEGRAM_2FA=<password> python tests/tg_auth.py")
            await client.disconnect()
            sys.exit(2)
        await client.sign_in(password=TWO_FA)

    me = await client.get_me()
    HASH_FILE.unlink(missing_ok=True)
    print(f"[OK] Signed in as @{me.username} ({me.first_name})")
    print("Session saved. You can now run: python tests/bot_automation.py")
    await client.disconnect()


asyncio.run(main())
