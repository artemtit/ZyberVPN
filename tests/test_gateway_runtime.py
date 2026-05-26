from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.services.gateway_config import GatewayConfigRenderer, GatewayConfigValidator
from app.services.gateway_runtime import GatewayRuntimeApplier, GatewayRuntimeManager
from app.services.vpn.base import ServerInfo


VALID_UPSTREAM = {
    "log": {"loglevel": "warning"},
    "outbounds": [
        {"tag": "edge-a", "protocol": "vless"},
        {"tag": "direct", "protocol": "freedom"},
        {"tag": "block", "protocol": "blackhole"},
    ],
    "routing": {
        "rules": [
            {"type": "field", "domain": ["example.com"], "outboundTag": "edge-a"},
            {"type": "field", "network": "tcp,udp", "outboundTag": "direct"},
        ]
    },
}


class GatewayConfigValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = GatewayConfigValidator()

    def test_accepts_valid_bundle(self) -> None:
        result = self.validator.validate_text(json.dumps(VALID_UPSTREAM))
        self.assertTrue(result.is_valid)
        self.assertEqual(result.errors, [])
        self.assertTrue(result.config_hash)

    def test_rejects_missing_referenced_balancer(self) -> None:
        payload = {
            **VALID_UPSTREAM,
            "routing": {
                "rules": [
                    {"type": "field", "balancerTag": "missing", "network": "tcp,udp"},
                ]
            },
        }
        result = self.validator.validate_text(json.dumps(payload))
        self.assertFalse(result.is_valid)
        self.assertIn("missing balancerTag 'missing'", "; ".join(result.errors))

    def test_rejects_missing_default_route(self) -> None:
        payload = {
            **VALID_UPSTREAM,
            "routing": {
                "rules": [
                    {"type": "field", "domain": ["example.com"], "outboundTag": "edge-a"},
                ]
            },
        }
        result = self.validator.validate_text(json.dumps(payload))
        self.assertFalse(result.is_valid)
        self.assertIn("default route", "; ".join(result.errors))


class GatewayRuntimeApplierTests(unittest.IsolatedAsyncioTestCase):
    async def test_skip_unchanged_config(self) -> None:
        restarts: list[str] = []

        async def restart_service(service_name: str) -> None:
            restarts.append(service_name)

        async def service_is_active(_: str) -> bool:
            return True

        server = ServerInfo(
            id=1,
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
            gateway_config_path="",
            gateway_service_name="zybervpn-gateway",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            server.gateway_config_path = str(Path(tmp_dir) / "gateway.json")
            applier = GatewayRuntimeApplier(
                restart_service=restart_service,
                service_is_active=service_is_active,
            )
            first = await applier.apply(
                server=server,
                rendered_config='{"hello":"world"}',
                config_hash="hash-a",
            )
            second = await applier.apply(
                server=server,
                rendered_config='{"hello":"world"}',
                config_hash="hash-a",
            )
            third = await applier.apply(
                server=server,
                rendered_config='{"hello":"mars"}',
                config_hash="hash-b",
            )

        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertTrue(third.changed)
        self.assertEqual(restarts, ["zybervpn-gateway", "zybervpn-gateway"])


class _FakeServersRepo:
    def __init__(self, gateways: list[ServerInfo]) -> None:
        self._gateways = gateways
        self.updates: list[tuple[int, str, str]] = []

    async def list_gateways(self) -> list[ServerInfo]:
        return list(self._gateways)

    async def update_gateway_apply(self, server_id: int, *, status: str, error_text: str = "", applied_at=None) -> None:
        self.updates.append((server_id, status, error_text))


class _FakeUpstreamsRepo:
    def __init__(self, row: dict | None) -> None:
        self.row = row
        self.validation_updates: list[tuple[int, str, str]] = []

    async def get_active(self) -> dict | None:
        return self.row

    async def update_validation(self, upstream_id: int, *, validation_status: str, validation_error: str = "", config_hash: str = "") -> None:
        self.validation_updates.append((upstream_id, validation_status, validation_error))

    async def mark_applied(self, upstream_id: int, *, config_hash: str) -> None:
        return None


class _FailingApplier:
    async def apply(self, *, server: ServerInfo, rendered_config: str, config_hash: str, force: bool = False):  # noqa: ARG002
        raise RuntimeError("reload failed")


class GatewayRuntimeManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_apply_error_is_persisted(self) -> None:
        gateway = ServerInfo(
            id=7,
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
            gateway_config_path="/tmp/gateway.json",
            gateway_service_name="zybervpn-gateway",
        )
        servers_repo = _FakeServersRepo([gateway])
        upstreams_repo = _FakeUpstreamsRepo(
            {
                "id": 1,
                "is_active": True,
                "source_kind": "db",
                "raw_json": json.dumps(VALID_UPSTREAM),
            }
        )
        manager = GatewayRuntimeManager(
            servers_repo=servers_repo,
            upstreams_repo=upstreams_repo,
            validator=GatewayConfigValidator(),
            renderer=GatewayConfigRenderer(),
            applier=_FailingApplier(),
        )

        summary = await manager.sync(force=True)

        self.assertEqual(summary.gateways_failed, 1)
        self.assertIn((7, "error", "reload failed"), servers_repo.updates)
