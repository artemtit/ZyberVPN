from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.services.gateway_config import (
    GatewayConfigRenderer,
    GatewayConfigValidator,
    build_effective_gateway_config,
)
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

BASE_XUI_CONFIG = {
    "log": {
        "access": "./access.log",
        "error": "./error.log",
        "loglevel": "info",
    },
    "routing": {
        "domainStrategy": "AsIs",
        "rules": [
            {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
            {"type": "field", "protocol": ["bittorrent"], "outboundTag": "blocked"},
        ],
    },
    "inbounds": [
        {"tag": "api", "protocol": "tunnel"},
        {"tag": "inbound-443", "protocol": "vless"},
    ],
    "outbounds": [
        {"tag": "direct", "protocol": "freedom", "settings": {"domainStrategy": "AsIs"}},
        {"tag": "blocked", "protocol": "blackhole"},
    ],
    "api": {"tag": "api", "services": ["HandlerService"]},
    "policy": {"levels": {"0": {"statsUserDownlink": True}}},
    "stats": {},
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
            first_payload = GatewayConfigRenderer().render(server, VALID_UPSTREAM)
            third_payload = GatewayConfigRenderer().render(
                server,
                {
                    **VALID_UPSTREAM,
                    "routing": {
                        "rules": [
                            {"type": "field", "domain": ["example.org"], "outboundTag": "edge-a"},
                            {"type": "field", "network": "tcp,udp", "outboundTag": "direct"},
                        ]
                    },
                },
            )
            first = await applier.apply(
                server=server,
                rendered_config=first_payload,
                config_hash="hash-a",
            )
            second = await applier.apply(
                server=server,
                rendered_config=first_payload,
                config_hash="hash-a",
            )
            third = await applier.apply(
                server=server,
                rendered_config=third_payload,
                config_hash="hash-b",
            )

        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertTrue(third.changed)
        self.assertEqual(restarts, ["zybervpn-gateway", "zybervpn-gateway"])

    async def test_rollback_restores_previous_config_on_restart_failure(self) -> None:
        states = {"active": True}
        restarts: list[str] = []

        async def restart_service(service_name: str) -> None:
            restarts.append(service_name)
            if len(restarts) == 1:
                raise RuntimeError("boom")

        async def service_is_active(_: str) -> bool:
            return states["active"]

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
            target = Path(tmp_dir) / "gateway.json"
            target.write_text(json.dumps(BASE_XUI_CONFIG), encoding="utf-8")
            server.gateway_config_path = str(target)
            applier = GatewayRuntimeApplier(
                restart_service=restart_service,
                service_is_active=service_is_active,
            )
            with self.assertRaisesRegex(RuntimeError, "boom"):
                await applier.apply(
                    server=server,
                    rendered_config=GatewayConfigRenderer().render(server, VALID_UPSTREAM),
                    config_hash="hash-a",
                )

            restored = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(restored, BASE_XUI_CONFIG)
        self.assertEqual(restarts, ["zybervpn-gateway", "zybervpn-gateway"])

    async def test_rollback_when_persisted_config_drifts(self) -> None:
        restarts: list[str] = []
        storage = {"content": json.dumps(BASE_XUI_CONFIG)}

        def file_reader(_: str) -> str:
            return storage["content"]

        def file_writer(_: str, content: str) -> None:
            storage["content"] = content

        async def restart_service(service_name: str) -> None:
            restarts.append(service_name)
            if len(restarts) == 1:
                storage["content"] = json.dumps(BASE_XUI_CONFIG)

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
            gateway_config_path="/tmp/gateway.json",
            gateway_service_name="zybervpn-gateway",
        )
        applier = GatewayRuntimeApplier(
            file_reader=file_reader,
            file_writer=file_writer,
            restart_service=restart_service,
            service_is_active=service_is_active,
        )

        with self.assertRaisesRegex(RuntimeError, "persisted config drift"):
            await applier.apply(
                server=server,
                rendered_config=GatewayConfigRenderer().render(server, VALID_UPSTREAM),
                config_hash="hash-a",
            )

        self.assertEqual(json.loads(storage["content"]), BASE_XUI_CONFIG)
        self.assertEqual(restarts, ["zybervpn-gateway", "zybervpn-gateway"])


class GatewayConfigMergeTests(unittest.TestCase):
    def test_merge_preserves_xui_inbounds_and_api(self) -> None:
        rendered_upstream = GatewayConfigRenderer().render(
            ServerInfo(
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
            ),
            {
                "log": {"loglevel": "error"},
                "outbounds": [
                    {"tag": "white", "protocol": "vless"},
                    {"tag": "direct", "protocol": "freedom", "settings": {"domainStrategy": "UseIP"}},
                ],
                "routing": {
                    "domainStrategy": "AsIs",
                    "balancers": [
                        {"tag": "white-balancer", "selector": ["white"]},
                    ],
                    "rules": [
                        {"type": "field", "domain": ["example.com"], "balancerTag": "white-balancer"},
                        {"type": "field", "network": "tcp,udp", "balancerTag": "white-balancer"},
                    ],
                },
            },
        )
        merged = json.loads(
            build_effective_gateway_config(
                json.dumps(BASE_XUI_CONFIG),
                rendered_upstream,
            )
        )

        self.assertEqual([inbound["tag"] for inbound in merged["inbounds"]], ["api", "inbound-443"])
        self.assertEqual(merged["api"]["tag"], "api")
        self.assertEqual(merged["log"]["access"], "./access.log")
        self.assertEqual(merged["log"]["loglevel"], "error")
        self.assertIn("white", {outbound["tag"] for outbound in merged["outbounds"]})
        direct_outbound = next(outbound for outbound in merged["outbounds"] if outbound["tag"] == "direct")
        self.assertEqual(direct_outbound["settings"]["domainStrategy"], "UseIP")
        self.assertEqual(merged["routing"]["rules"][0]["outboundTag"], "api")
        self.assertEqual(merged["routing"]["rules"][-1]["balancerTag"], "white-balancer")


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
