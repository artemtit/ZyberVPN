"""Diagnostic: sends /start and prints raw event data from bot."""
import asyncio, os, sys
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv()

from telethon import TelegramClient, events
from telethon.sessions import StringSession

API_ID  = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
SESSION = os.getenv("TELEGRAM_SESSION", "")
BOT     = "Zyber_VPN_bot"

async def main():
    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    async with client:
        bot = await client.get_entity(BOT)
        print(f"Bot entity: id={bot.id} username={bot.username}", flush=True)

        received = []

        @client.on(events.NewMessage())
        async def on_new(event):
            m = event.message
            print(f"[NewMessage] sender={m.sender_id} chat={m.chat_id} date={m.date} text={repr((m.text or '')[:80])}", flush=True)
            received.append(m)

        @client.on(events.MessageEdited())
        async def on_edit(event):
            m = event.message
            print(f"[MsgEdited]  sender={m.sender_id} chat={m.chat_id} date={m.date} text={repr((m.text or '')[:80])}", flush=True)
            received.append(m)

        print(f"Sending /start at {datetime.now(timezone.utc).isoformat()}", flush=True)
        await client.send_message(bot, "/start")
        print("Waiting 10s for response...", flush=True)
        await asyncio.sleep(10)
        print(f"Done. Got {len(received)} events total.", flush=True)

asyncio.run(main())
