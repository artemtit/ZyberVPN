from __future__ import annotations

import unittest

from app.services.vpn.base import ServerInfo
from app.services.vpn.manager import VPNManager, pick_server
from app.services.vpn.xui_provider import XUIProvider


class PickServerGatewayTests(unittest.TestCase):
    def test_gateway_requires_ready_apply_status(self) -> None:
        direct = ServerInfo(
            id=1,
            name="direct",
            host="direct.example",
            api_url="http://127.0.0.1:2053",
            username="u",
            password="p",
            inbound_id=1,
            public_key="pk",
            short_id="sid",
            country="NL",
            is_active=True,
            server_role="direct",
        )
        gateway = ServerInfo(
            id=2,
            name="gateway",
            host="gateway.example",
            api_url="http://127.0.0.1:2053",
            username="u",
            password="p",
            inbound_id=1,
            public_key="pk",
            short_id="sid",
            country="NL",
            is_active=True,
            server_role="gateway",
            gateway_apply_status="error",
        )

        candidates = pick_server([gateway, direct], user_counts={}, block_minutes=5)

        self.assertEqual([server.id for server in candidates], [1])


class _FakeUserVpnRepo:
    def __init__(self, primary_row: dict, secondary_rows: list[dict]) -> None:
        self.primary_row = primary_row
        self.secondary_rows = secondary_rows
        self.status_updates: list[tuple[int, str, int | None]] = []

    async def get_user_vpn(self, user_id: int, key_id: int | None = None) -> dict | None:  # noqa: ARG002
        return self.primary_row

    async def list_secondary_for_key(self, user_id: int, key_id: int) -> list[dict]:  # noqa: ARG002
        return list(self.secondary_rows)

    async def set_status(self, user_id: int, status: str, key_id: int | None = None) -> None:
        self.status_updates.append((user_id, status, key_id))


class _FakeServersRepo:
    def __init__(self, servers: list[ServerInfo]) -> None:
        self.servers = servers

    async def list_all(self) -> list[ServerInfo]:
        return list(self.servers)


class _FakeVpnDevicesRepo:
    async def count_recent_devices(self, *, user_id: int, key_id: int, window_hours: int = 24) -> int:  # noqa: ARG002
        return 3


class _FakeKeysRepo:
    async def get_traffic_limit_gb(self, key_id: int, user_id: int) -> int | None:  # noqa: ARG002
        return 1


class _FakeXUIProvider(XUIProvider):
    def __init__(self, traffic_by_server: dict[int, dict[str, dict[str, int]]]) -> None:
        super().__init__()
        self.traffic_by_server = traffic_by_server
        self.disabled: list[tuple[int, str]] = []

    async def get_client_traffic(self, server: ServerInfo, email: str) -> dict | None:  # noqa: ARG002
        bucket = "ws" if email.endswith("_ws") else "reality"
        return dict(self.traffic_by_server.get(server.id, {}).get(bucket, {}))

    async def disable_client(self, server: ServerInfo, client_uuid: str) -> None:
        self.disabled.append((server.id, client_uuid))


class VPNManagerGatewayAggregationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.server_primary = ServerInfo(
            id=1,
            name="direct",
            host="direct.example",
            api_url="http://127.0.0.1:2053",
            username="u",
            password="p",
            inbound_id=1,
            public_key="pk",
            short_id="sid",
            country="NL",
            is_active=True,
        )
        self.server_secondary = ServerInfo(
            id=2,
            name="gateway",
            host="gateway.example",
            api_url="http://127.0.0.1:2054",
            username="u",
            password="p",
            inbound_id=1,
            public_key="pk",
            short_id="sid",
            country="PL",
            is_active=True,
            server_role="gateway",
            gateway_apply_status="ready",
        )
        self.primary_row = {
            "user_id": 100,
            "server_id": 1,
            "status": "ready",
            "key_id": 5,
            "reality_uuid": "uuid-primary",
            "ws_uuid": "ws-primary",
        }
        self.secondary_rows = [
            {
                "user_id": 100,
                "server_id": 2,
                "status": "ready",
                "key_id": 9_000_050_002,
                "reality_uuid": "uuid-secondary",
                "ws_uuid": "ws-secondary",
            }
        ]

    async def test_get_client_stats_sums_all_server_rows(self) -> None:
        provider = _FakeXUIProvider(
            {
                1: {
                    "reality": {"up": 100, "down": 200, "enable": True},
                    "ws": {"up": 0, "down": 0, "enable": True},
                },
                2: {
                    "reality": {"up": 300, "down": 400, "enable": True},
                    "ws": {"up": 0, "down": 0, "enable": True},
                },
            }
        )
        manager = VPNManager(
            providers={"xui": provider},
            servers_repo=_FakeServersRepo([self.server_primary, self.server_secondary]),
            user_vpn_repo=_FakeUserVpnRepo(self.primary_row, self.secondary_rows),
            vpn_devices_repo=_FakeVpnDevicesRepo(),
            settings=type("Settings", (), {"xray_device_window_hours": 24})(),
        )

        total_bytes, unique_devices = await manager.get_client_stats(100, key_id=5)

        self.assertEqual(total_bytes, 1000)
        self.assertEqual(unique_devices, 3)

    async def test_enforce_traffic_limit_disables_all_server_rows(self) -> None:
        provider = _FakeXUIProvider(
            {
                1: {
                    "reality": {
                        "up": 700 * 1024 ** 2,
                        "down": 100 * 1024 ** 2,
                        "enable": True,
                        "total": 0,
                    },
                    "ws": {"up": 0, "down": 0, "enable": True},
                },
                2: {
                    "reality": {
                        "up": 300 * 1024 ** 2,
                        "down": 100 * 1024 ** 2,
                        "enable": True,
                        "total": 0,
                    },
                    "ws": {"up": 0, "down": 0, "enable": True},
                },
            }
        )
        user_vpn_repo = _FakeUserVpnRepo(self.primary_row, self.secondary_rows)
        manager = VPNManager(
            providers={"xui": provider},
            servers_repo=_FakeServersRepo([self.server_primary, self.server_secondary]),
            user_vpn_repo=user_vpn_repo,
            vpn_devices_repo=None,
            settings=type("Settings", (), {"vpn_total_gb": 1, "xray_device_window_hours": 24})(),
            users_repo=object(),
            keys_repo=_FakeKeysRepo(),
            bot=None,
        )

        disabled = await manager.enforce_traffic_limit(100, key_id=5)

        self.assertTrue(disabled)
        self.assertEqual(
            sorted(provider.disabled),
            [
                (1, "uuid-primary"),
                (1, "ws-primary"),
                (2, "uuid-secondary"),
                (2, "ws-secondary"),
            ],
        )
        self.assertIn((100, "limit_exceeded", 5), user_vpn_repo.status_updates)
