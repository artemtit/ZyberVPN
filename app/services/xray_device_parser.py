from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from app.config import Settings
from app.db.database import Database
from app.repositories.vpn_devices import VpnDevicesRepository
from app.utils.datetime import parse_iso_utc, utc_now

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^user_(?P<user_id>\d+)_(?P<key_id>\d+)(?:_ws)?$")
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
UA_RE = re.compile(r'(?:user-agent|user_agent|ua|client)[=:"\s]+(?P<ua>[^",\]}]+)', re.IGNORECASE)


@dataclass(slots=True)
class ParserState:
    inode: int
    offset: int


def normalize_user_agent(user_agent: str) -> str:
    return " ".join((user_agent or "").strip().lower().split())


def build_device_hash(user_agent: str) -> str:
    normalized = normalize_user_agent(user_agent)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def parse_keyed_email(email: str) -> tuple[int, int] | None:
    match = EMAIL_RE.match((email or "").strip())
    if not match:
        return None
    return int(match.group("user_id")), int(match.group("key_id"))


def parse_log_line(line: str) -> dict | None:
    line = (line or "").strip()
    if not line:
        return None

    now = utc_now()
    email = ""
    user_agent = ""
    ip = ""
    seen_at = now

    if line.startswith("{") and line.endswith("}"):
        try:
            payload = json.loads(line)
            email = str(payload.get("email") or payload.get("user") or "").strip()
            user_agent = str(payload.get("user-agent") or payload.get("user_agent") or payload.get("ua") or "").strip()
            ip = str(payload.get("ip") or payload.get("client_ip") or payload.get("source") or "").strip()
            ts = payload.get("time") or payload.get("timestamp") or payload.get("@timestamp")
            if ts:
                try:
                    seen_at = parse_iso_utc(str(ts))
                except Exception:
                    pass
        except Exception:
            return None

    if not email:
        email_match = re.search(r"\buser_\d+_\d+(?:_ws)?\b", line)
        if email_match:
            email = email_match.group(0)

    if not user_agent:
        ua_match = UA_RE.search(line)
        if ua_match:
            user_agent = ua_match.group("ua").strip()
        elif "mozilla/" in line.lower() or "v2ray" in line.lower() or "happ" in line.lower():
            user_agent = line[-160:].strip()

    if not ip:
        ip_match = IP_RE.search(line)
        if ip_match:
            ip = ip_match.group(0)

    keyed = parse_keyed_email(email)
    if not keyed:
        return None
    if not normalize_user_agent(user_agent):
        return None

    user_id, key_id = keyed
    return {
        "user_id": user_id,
        "key_id": key_id,
        "email": email,
        "user_agent": user_agent,
        "ip": ip,
        "seen_at": seen_at.isoformat(),
    }


class XrayDeviceParser:
    def __init__(self, db: Database, settings: Settings) -> None:
        self._repo = VpnDevicesRepository(db)
        self._settings = settings
        self._state_file = Path(settings.xray_parser_state_path)
        self._server_id = settings.xray_parser_server_id

    def _load_state(self) -> ParserState:
        if not self._state_file.exists():
            return ParserState(inode=0, offset=0)
        try:
            payload = json.loads(self._state_file.read_text(encoding="utf-8"))
            return ParserState(
                inode=int(payload.get("inode") or 0),
                offset=int(payload.get("offset") or 0),
            )
        except Exception:
            return ParserState(inode=0, offset=0)

    def _save_state(self, state: ParserState) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(
            json.dumps({"inode": state.inode, "offset": state.offset}),
            encoding="utf-8",
        )

    def _read_cycle_lines(self) -> list[str]:
        path = self._settings.xray_access_log_path
        if not os.path.exists(path):
            return []

        state = self._load_state()
        max_bytes = self._settings.xray_parser_max_bytes_per_cycle
        max_lines = self._settings.xray_parser_max_lines_per_cycle

        st = os.stat(path)
        inode = int(st.st_ino)
        size = int(st.st_size)

        start = state.offset
        if inode != state.inode or start > size:
            start = max(0, size - max_bytes)

        read_len = min(max_bytes, max(0, size - start))
        if read_len <= 0:
            self._save_state(ParserState(inode=inode, offset=size))
            return []

        with open(path, "rb") as f:
            f.seek(start)
            raw = f.read(read_len)

        new_offset = start + len(raw)
        self._save_state(ParserState(inode=inode, offset=new_offset))

        lines = raw.decode("utf-8", errors="replace").splitlines()
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        return lines

    async def run_once(self) -> int:
        if self._server_id <= 0:
            logger.warning("XRAY parser skipped: invalid XRAY_PARSER_SERVER_ID=%s", self._server_id)
            return 0

        lines = self._read_cycle_lines()
        if not lines:
            return 0

        cutoff = utc_now() - timedelta(hours=max(1, self._settings.xray_device_window_hours))
        dedup: dict[tuple[int, int, str], dict] = {}

        for line in lines:
            parsed = parse_log_line(line)
            if not parsed:
                continue
            try:
                seen_at = parse_iso_utc(parsed["seen_at"])
            except Exception:
                seen_at = utc_now()
            if seen_at < cutoff:
                continue

            device_hash = build_device_hash(parsed["user_agent"])
            key = (self._server_id, int(parsed["key_id"]), device_hash)
            existing = dedup.get(key)
            payload = {
                "server_id": self._server_id,
                "user_id": int(parsed["user_id"]),
                "key_id": int(parsed["key_id"]),
                "email": str(parsed["email"]),
                "device_hash": device_hash,
                "user_agent": str(parsed["user_agent"]),
                "ip": str(parsed["ip"]),
                "last_seen": seen_at.isoformat(),
                "updated_at": utc_now().isoformat(),
            }
            if not existing or existing["last_seen"] < payload["last_seen"]:
                dedup[key] = payload

        if not dedup:
            return 0

        rows = list(dedup.values())
        affected = await self._repo.batch_upsert(rows, chunk_size=1000)
        logger.info(
            "xray parser upsert completed server_id=%s lines=%s rows=%s affected=%s",
            self._server_id,
            len(lines),
            len(rows),
            affected,
        )
        return affected
