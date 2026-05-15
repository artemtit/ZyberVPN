"""
ZyberVPN Bot Automation Tester

Connects to Telegram via user session (Telethon) and runs automated tests
against @ZyberVPNBot to catch handler bugs.

Setup:
    pip install telethon
    Add to .env: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE

Usage:
    python tests/bot_automation.py
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

try:
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession
    from telethon.tl.custom import Message
    from telethon.tl.types import InputPeerUser
    from telethon.tl.functions.messages import GetBotCallbackAnswerRequest
    from telethon.errors import FloodWaitError
except ImportError:
    print("Telethon not installed. Run: pip install telethon")
    sys.exit(1)

# ─── Config ──────────────────────────────────────────────────────────────────
BOT_USERNAME    = "Zyber_VPN_bot"
API_ID          = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH        = os.getenv("TELEGRAM_API_HASH", "")
PHONE           = os.getenv("TELEGRAM_PHONE", "")
SESSION_STRING  = os.getenv("TELEGRAM_SESSION", "")
SESSION_FILE    = Path(__file__).parent / "bot_test_session"
TIMEOUT         = 15      # seconds per attempt
COLLECT_WINDOW  = 0.5     # seconds to buffer multiple events before picking latest

BUTTON_WHITELIST_PREFIXES = (
    "buy_", "menu_", "profile_", "key_", "topup_",
    "tariff:", "pay:", "legal_", "back_", "device_",
    "app_", "promo_", "ref_", "connect_", "noop",
)

ERROR_PATTERNS = [
    "ошибка", "error", "traceback", "500",
    "что-то пошло не так", "exception",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ─── Test Result ─────────────────────────────────────────────────────────────
class TestResult:
    def __init__(self, name: str):
        self.name         = name
        self.status       = "PENDING"
        self.error        = None
        self.details      = ""
        self.raw_response = None
        self.duration_ms  = 0
        self.timestamp    = _utcnow().isoformat()
        self._start       = time.monotonic()

    def _finish(self):
        self.duration_ms = int((time.monotonic() - self._start) * 1000)

    def passed(self, details: str = ""):
        self.status  = "PASS"
        self.details = details
        self._finish()

    def failed(self, reason: str, raw: str | None = None):
        self.status       = "FAIL"
        self.error        = reason
        self.raw_response = raw
        self._finish()

    def skipped(self, reason: str):
        self.status  = "SKIP"
        self.details = reason
        self._finish()


# ─── Bot Tester ──────────────────────────────────────────────────────────────
class BotTester:
    def __init__(self, client: TelegramClient):
        self.client           = client
        self.bot              = None
        self.last_action_ts   = _utcnow()
        self.last_message_id: int | None = None
        self._pending: asyncio.Future | None = None
        self._buf: list       = []
        self._collect_scheduled = False
        self.results: list[TestResult] = []

    async def setup(self) -> None:
        self.bot = await self.client.get_entity(BOT_USERNAME)
        bot_id = self.bot.id

        async def _on_bot_message(event):
            msg = event.message
            # Filter: must be from bot in our private chat
            if msg.sender_id != bot_id:
                return
            if msg.chat_id != bot_id:
                return
            # Dedup: skip if same message id as last processed
            if msg.id == self.last_message_id:
                return
            # Skip if no pending future (we're not waiting for anything)
            if self._pending is None or self._pending.done():
                return
            self._buf.append(msg)
            if not self._collect_scheduled:
                self._collect_scheduled = True
                asyncio.get_event_loop().call_later(COLLECT_WINDOW, self._flush_buf)

        self.client.add_event_handler(_on_bot_message, events.NewMessage())
        self.client.add_event_handler(_on_bot_message, events.MessageEdited())

    def _flush_buf(self) -> None:
        self._collect_scheduled = False
        if not self._buf:
            return
        if self._pending is None or self._pending.done():
            self._buf.clear()
            return
        best = max(self._buf, key=lambda m: m.id)
        self._buf.clear()
        self.last_message_id = best.id
        self._pending.set_result(best)

    def _new_future(self) -> asyncio.Future:
        self._buf.clear()
        self._collect_scheduled = False
        self.last_message_id = None  # allow same msg_id through (bot edits original message)
        self._pending = asyncio.get_event_loop().create_future()
        return self._pending

    async def _wait_response(self, timeout: int = TIMEOUT) -> Message:
        try:
            return await asyncio.wait_for(asyncio.shield(self._pending), timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"No bot response in {timeout}s")

    async def _with_retry(self, action_coro_fn, timeout: int = TIMEOUT) -> Message:
        delays = [1, 2, 4]
        for attempt, delay in enumerate(delays, start=1):
            self._new_future()
            self.last_action_ts = _utcnow()
            try:
                await action_coro_fn()
                return await self._wait_response(timeout)
            except FloodWaitError as e:
                wait = min(e.seconds + 1, 60)
                print(f"    [..] FloodWait {e.seconds}s — sleeping {wait}s...", flush=True)
                await asyncio.sleep(wait)
                if attempt == len(delays):
                    raise
            except TimeoutError:
                if attempt == len(delays):
                    raise TimeoutError(f"No response after {len(delays)} attempts")
                print(f"    [..] Timeout (attempt {attempt}/{len(delays)}), retrying in {delay}s...", flush=True)
                await asyncio.sleep(delay)

    async def send(self, text: str) -> Message:
        self._log_action("send", text)

        async def _do():
            await self.client.send_message(self.bot, text)

        msg = await self._with_retry(_do)
        self._log_response(msg)
        return msg

    async def click(self, msg: Message, callback_data: str) -> Message:
        # Stale guard
        if msg.id != self.last_message_id:
            raise ValueError(
                f"Stale message: msg.id={msg.id} != last_message_id={self.last_message_id}. "
                "Always use the return value of send()/click() directly."
            )
        # Whitelist guard
        if not any(callback_data.startswith(p) for p in BUTTON_WHITELIST_PREFIXES):
            raise ValueError(f"callback_data '{callback_data}' not in whitelist")

        # Find button row/col indices
        row_i, col_j = None, None
        if msg.reply_markup:
            for i, row in enumerate(msg.reply_markup.rows):
                for j, btn in enumerate(row.buttons):
                    if hasattr(btn, "data") and btn.data.decode() == callback_data:
                        row_i, col_j = i, j
                        break
                if row_i is not None:
                    break
        if row_i is None:
            available = self.get_buttons(msg)
            raise ValueError(f"Button '{callback_data}' not found. Available: {available}")

        btn_data = msg.reply_markup.rows[row_i].buttons[col_j].data
        peer = InputPeerUser(self.bot.id, self.bot.access_hash)

        self._log_action("click", callback_data)

        async def _do():
            await self.client(GetBotCallbackAnswerRequest(
                peer=peer,
                msg_id=msg.id,
                data=btn_data,
            ))

        result = await self._with_retry(_do)
        self._log_response(result)
        return result

    def _get_raw_buttons(self, msg: Message) -> list[bytes]:
        if msg.reply_markup is None:
            return []
        result = []
        for row in msg.reply_markup.rows:
            for btn in row.buttons:
                if hasattr(btn, "data"):
                    result.append(btn.data)
        return result

    def get_buttons(self, msg: Message) -> list[str]:
        if msg.reply_markup is None:
            return []
        result = []
        for row in msg.reply_markup.rows:
            for btn in row.buttons:
                if hasattr(btn, "data"):
                    result.append(btn.data.decode())
        return result

    def check_for_errors(self, msg: Message) -> str | None:
        text_lower = (msg.text or "").lower()
        for p in ERROR_PATTERNS:
            if p in text_lower:
                return f"Error pattern '{p}' found in response"
        return None

    def assert_buttons(self, msg: Message, expected: list[str]) -> None:
        btns = self.get_buttons(msg)
        missing = []
        for e in expected:
            if e.endswith("*"):
                prefix = e[:-1]
                if not any(b.startswith(prefix) for b in btns):
                    missing.append(e)
            else:
                if e not in btns:
                    missing.append(e)
        if missing:
            raise AssertionError(f"Expected buttons missing: {missing}. Got: {btns}")

    async def reset(self) -> Message:
        self.last_message_id = None
        self._buf.clear()
        self._collect_scheduled = False
        self._pending = None
        msg = await self.send("/start")
        await asyncio.sleep(2)  # avoid FloodWait between resets
        return msg

    @staticmethod
    def _safe(s: str, limit: int = 200) -> str:
        s = (s or "")[:limit].replace("\n", " ")
        return s.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8"
        )

    def _log_action(self, kind: str, data: str) -> None:
        print(f"    [ACTION] {kind} {data!r}", flush=True)

    def _log_response(self, msg: Message) -> None:
        text = self._safe(msg.text or "")
        btns = self.get_buttons(msg)[:10]
        print(f"    [RESPONSE] text={text!r} | buttons={btns} | msg_id={msg.id}", flush=True)

    def _add(self, name: str) -> TestResult:
        r = TestResult(name)
        self.results.append(r)
        return r

    def _log_result(self, r: TestResult) -> None:
        sym = {"PASS": "[+]", "FAIL": "[!]", "SKIP": "[-]"}.get(r.status, "[?]")
        print(f"  {sym} [{r.status}] {r.name} ({r.duration_ms}ms)", flush=True)
        if r.error:
            print(f"       Error: {self._safe(r.error, 300)}", flush=True)
        if r.details:
            print(f"       Info: {self._safe(r.details, 300)}", flush=True)

    # ─── Test Scenarios ───────────────────────────────────────────────────────

    async def test_start_command(self) -> None:
        r = self._add("01_start_command")
        msg = None
        try:
            msg = await self.reset()
            if err := self.check_for_errors(msg):
                r.failed(err, msg.text)
            else:
                self.assert_buttons(msg, ["buy_open", "menu_keys", "menu_profile", "legal_docs"])
                r.passed(f"Buttons: {self.get_buttons(msg)}")
        except Exception as e:
            r.failed(str(e), msg.text if msg else None)
        self._log_result(r)

    async def test_keys_list(self) -> None:
        r = self._add("02_keys_list")
        msg = None
        try:
            msg = await self.reset()
            msg = await self.click(msg, "menu_keys")
            if err := self.check_for_errors(msg):
                r.failed(err, msg.text)
            else:
                self.assert_buttons(msg, ["back_menu"])
                r.passed(f"Keys screen loaded. Buttons: {self.get_buttons(msg)}")
        except Exception as e:
            r.failed(str(e), msg.text if msg else None)
        self._log_result(r)

    async def test_profile(self) -> None:
        r = self._add("03_profile")
        msg = None
        try:
            msg = await self.reset()
            msg = await self.click(msg, "menu_profile")
            if err := self.check_for_errors(msg):
                r.failed(err, msg.text)
            else:
                self.assert_buttons(msg, ["profile_ref", "profile_promo", "profile_topup", "back_menu"])
                r.passed(f"Profile loaded. Buttons: {self.get_buttons(msg)}")
        except Exception as e:
            r.failed(str(e), msg.text if msg else None)
        self._log_result(r)

    async def test_purchase_open(self) -> None:
        r = self._add("04_purchase_open")
        msg = None
        try:
            msg = await self.reset()
            msg = await self.click(msg, "buy_open")
            if err := self.check_for_errors(msg):
                r.failed(err, msg.text)
            else:
                self.assert_buttons(msg, ["buy_plan:*"])
                btns = [b for b in self.get_buttons(msg) if b.startswith("buy_plan:")]
                r.passed(f"Plans found: {btns}")
        except Exception as e:
            r.failed(str(e), msg.text if msg else None)
        self._log_result(r)

    async def test_purchase_payment_methods(self) -> None:
        r = self._add("05_purchase_payment_methods")
        msg = None
        try:
            msg = await self.reset()
            msg = await self.click(msg, "buy_open")
            plan_btns = [b for b in self.get_buttons(msg) if b.startswith("buy_plan:")]
            if not plan_btns:
                r.skipped("No buy_plan buttons available")
                self._log_result(r)
                return
            msg = await self.click(msg, plan_btns[0])
            if err := self.check_for_errors(msg):
                r.failed(err, msg.text)
                self._log_result(r)
                return
            # Some flows require an extra tariff: click
            tariff_btns = [b for b in self.get_buttons(msg) if b.startswith("tariff:")]
            if tariff_btns:
                msg = await self.click(msg, tariff_btns[0])
                if err := self.check_for_errors(msg):
                    r.failed(err, msg.text)
                    self._log_result(r)
                    return
            self.assert_buttons(msg, ["pay:*"])
            r.passed(f"Payment methods screen loaded. Buttons: {self.get_buttons(msg)}")
        except Exception as e:
            r.failed(str(e), msg.text if msg else None)
        self._log_result(r)

    async def test_legal_docs(self) -> None:
        r = self._add("06_legal_docs")
        msg = None
        try:
            msg = await self.reset()
            msg = await self.click(msg, "legal_docs")
            if err := self.check_for_errors(msg):
                r.failed(err, msg.text)
            else:
                self.assert_buttons(msg, ["back_menu"])
                r.passed("Legal docs loaded")
        except Exception as e:
            r.failed(str(e), msg.text if msg else None)
        self._log_result(r)

    async def test_back_navigation(self) -> None:
        r = self._add("07_back_navigation")
        msg = None
        try:
            msg = await self.reset()
            msg = await self.click(msg, "menu_profile")
            msg = await self.click(msg, "back_menu")
            if err := self.check_for_errors(msg):
                r.failed(err, msg.text)
            else:
                self.assert_buttons(msg, ["buy_open", "menu_keys", "menu_profile", "legal_docs"])
                r.passed("Back navigation to main menu works")
        except Exception as e:
            r.failed(str(e), msg.text if msg else None)
        self._log_result(r)

    async def test_promo_invalid_code(self) -> None:
        r = self._add("08_promo_invalid_code")
        msg = None
        try:
            msg = await self.reset()
            msg = await self.click(msg, "menu_profile")
            if "profile_promo" not in self.get_buttons(msg):
                r.failed(f"profile_promo button not found. Got: {self.get_buttons(msg)}")
                self._log_result(r)
                return
            msg = await self.click(msg, "profile_promo")
            if err := self.check_for_errors(msg):
                r.failed(err, msg.text)
                self._log_result(r)
                return
            msg = await self.send("INVALID_PROMO_XYZ999")
            text = msg.text or ""
            if "не найден" in text or "❌" in text or "не существует" in text:
                r.passed("Invalid promo correctly rejected")
            elif err := self.check_for_errors(msg):
                r.failed(err, text)
            else:
                r.failed(f"Unexpected response to invalid promo: {text[:300]}", text)
        except Exception as e:
            r.failed(str(e), msg.text if msg else None)
        self._log_result(r)

    async def test_key_detail(self) -> None:
        r = self._add("09_key_detail")
        msg = None
        try:
            msg = await self.reset()
            msg = await self.click(msg, "menu_keys")
            key_btns = [b for b in self.get_buttons(msg) if b.startswith("key_open:")]
            if not key_btns:
                r.skipped("No keys present for this user")
                self._log_result(r)
                return
            key_id = key_btns[0].split(":", 1)[1]
            msg = await self.click(msg, key_btns[0])
            if err := self.check_for_errors(msg):
                r.failed(err, msg.text)
            else:
                self.assert_buttons(
                    msg,
                    [f"key_connect:{key_id}", f"key_qr:{key_id}", f"key_renew:{key_id}"],
                )
                r.passed(f"Key detail loaded for key {key_id}. Buttons: {self.get_buttons(msg)}")
        except Exception as e:
            r.failed(str(e), msg.text if msg else None)
        self._log_result(r)

    async def test_connect_flow(self) -> None:
        r = self._add("10_connect_flow")
        msg = None
        try:
            msg = await self.reset()
            msg = await self.click(msg, "menu_keys")
            key_btns = [b for b in self.get_buttons(msg) if b.startswith("key_open:")]
            if not key_btns:
                r.skipped("No keys present for this user")
                self._log_result(r)
                return
            key_id = key_btns[0].split(":", 1)[1]
            msg = await self.click(msg, key_btns[0])
            connect_btn = f"key_connect:{key_id}"
            if connect_btn not in self.get_buttons(msg):
                r.failed(f"key_connect button not found. Got: {self.get_buttons(msg)}", msg.text)
                self._log_result(r)
                return
            msg = await self.click(msg, connect_btn)
            if err := self.check_for_errors(msg):
                r.failed(err, msg.text)
                self._log_result(r)
                return
            device_btns = [b for b in self.get_buttons(msg) if b.startswith("device_")]
            if not device_btns:
                r.failed(f"No device buttons found. Got: {self.get_buttons(msg)}", msg.text)
                self._log_result(r)
                return
            target_device = "device_android" if "device_android" in device_btns else device_btns[0]
            msg = await self.click(msg, target_device)
            if err := self.check_for_errors(msg):
                r.failed(err, msg.text)
                self._log_result(r)
                return
            app_btns = [b for b in self.get_buttons(msg) if b.startswith("app_")]
            if app_btns:
                target_app = "app_v2rayng" if "app_v2rayng" in app_btns else app_btns[0]
                msg = await self.click(msg, target_app)
                if err := self.check_for_errors(msg):
                    r.failed(err, msg.text)
                    self._log_result(r)
                    return
            r.passed(f"Connect flow completed. Final buttons: {self.get_buttons(msg)}")
        except Exception as e:
            r.failed(str(e), msg.text if msg else None)
        self._log_result(r)

    async def test_referral_program(self) -> None:
        r = self._add("11_referral_program")
        msg = None
        try:
            msg = await self.reset()
            msg = await self.click(msg, "menu_profile")
            if "profile_ref" not in self.get_buttons(msg):
                r.failed(f"profile_ref button not found. Got: {self.get_buttons(msg)}")
                self._log_result(r)
                return
            msg = await self.click(msg, "profile_ref")
            if err := self.check_for_errors(msg):
                r.failed(err, msg.text)
            else:
                self.assert_buttons(msg, ["back_menu"])
                r.passed("Referral screen loaded")
        except Exception as e:
            r.failed(str(e), msg.text if msg else None)
        self._log_result(r)

    async def test_topup_stars(self) -> None:
        r = self._add("12_topup_stars")
        msg = None
        try:
            msg = await self.reset()
            msg = await self.click(msg, "menu_profile")
            if "profile_topup" not in self.get_buttons(msg):
                r.failed(f"profile_topup button not found. Got: {self.get_buttons(msg)}")
                self._log_result(r)
                return
            msg = await self.click(msg, "profile_topup")
            if err := self.check_for_errors(msg):
                r.failed(err, msg.text)
            else:
                self.assert_buttons(msg, ["topup_stars:100", "topup_stars:300"])
                stars_btns = [b for b in self.get_buttons(msg) if b.startswith("topup_stars:")]
                r.passed(f"Topup screen loaded. Stars options: {stars_btns}")
        except Exception as e:
            r.failed(str(e), msg.text if msg else None)
        self._log_result(r)

    # ─── Runner ───────────────────────────────────────────────────────────────

    async def run_all(self) -> bool:
        print(f"\n[BOT] ZyberVPN Bot Automation Tests")
        print(f"{'=' * 50}")
        print(f"Bot:  @{BOT_USERNAME}")
        print(f"Time: {_utcnow().isoformat()}")
        print(f"{'=' * 50}")

        await self.setup()

        tests = [
            self.test_start_command,
            self.test_keys_list,
            self.test_profile,
            self.test_purchase_open,
            self.test_purchase_payment_methods,
            self.test_legal_docs,
            self.test_back_navigation,
            self.test_promo_invalid_code,
            self.test_key_detail,
            self.test_connect_flow,
            self.test_referral_program,
            self.test_topup_stars,
        ]

        for test_fn in tests:
            print(f"\n  >> {test_fn.__name__}")
            await test_fn()
            await asyncio.sleep(3)

        passed  = sum(1 for r in self.results if r.status == "PASS")
        failed  = sum(1 for r in self.results if r.status == "FAIL")
        skipped = sum(1 for r in self.results if r.status == "SKIP")
        total   = len(self.results)

        print(f"\n{'=' * 50}")
        print(f"Results: {passed}/{total} passed | {failed} failed | {skipped} skipped")

        report_path = Path(__file__).parent / "bot_automation_results.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {
                        "name":         r.name,
                        "status":       r.status,
                        "error":        r.error,
                        "details":      r.details,
                        "raw_response": r.raw_response,
                        "duration_ms":  r.duration_ms,
                        "timestamp":    r.timestamp,
                    }
                    for r in self.results
                ],
                f,
                ensure_ascii=False,
                indent=2,
            )
        print(f"Report: {report_path}")
        return failed == 0


# ─── Entry Point ─────────────────────────────────────────────────────────────
async def main() -> None:
    if not API_ID or not API_HASH:
        print("[!] Missing TELEGRAM_API_ID or TELEGRAM_API_HASH in .env")
        print("    Get them at https://my.telegram.org/apps")
        sys.exit(1)

    session = StringSession(SESSION_STRING) if SESSION_STRING else str(SESSION_FILE)
    client = TelegramClient(session, API_ID, API_HASH)

    code_from_env = os.getenv("TELEGRAM_CODE", "").strip()

    async with client:
        if not SESSION_STRING:
            await client.start(
                phone=PHONE or input("Phone number: "),
                code_callback=lambda: code_from_env if code_from_env else input("Telegram code: "),
            )

        tester = BotTester(client)
        success = await tester.run_all()
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
