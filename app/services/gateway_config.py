from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from copy import deepcopy

from app.services.vpn.base import ServerInfo


@dataclass(slots=True)
class GatewayValidationResult:
    is_valid: bool
    payload: dict | None
    errors: list[str]
    config_hash: str


class GatewayConfigValidator:
    def validate_text(self, raw_text: str) -> GatewayValidationResult:
        config_hash = hashlib.sha256((raw_text or "").encode("utf-8")).hexdigest()
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as error:
            return GatewayValidationResult(
                is_valid=False,
                payload=None,
                errors=[f"Invalid JSON: {error.msg}"],
                config_hash=config_hash,
            )

        errors = self._validate_payload(payload)
        return GatewayValidationResult(
            is_valid=not errors,
            payload=payload if isinstance(payload, dict) else None,
            errors=errors,
            config_hash=config_hash,
        )

    def _validate_payload(self, payload: object) -> list[str]:
        if not isinstance(payload, dict):
            return ["Gateway upstream must be a JSON object"]

        errors: list[str] = []
        outbounds = payload.get("outbounds")
        if not isinstance(outbounds, list) or not outbounds:
            return ["Upstream config must contain at least one outbound"]

        outbound_tags: set[str] = set()
        proxy_graph: dict[str, str] = {}
        for index, outbound in enumerate(outbounds):
            if not isinstance(outbound, dict):
                errors.append(f"Outbound #{index + 1} must be an object")
                continue
            tag = str(outbound.get("tag") or "").strip()
            protocol = str(outbound.get("protocol") or "").strip()
            if not tag:
                errors.append(f"Outbound #{index + 1} has empty tag")
            if not protocol:
                errors.append(f"Outbound '{tag or index + 1}' has empty protocol")
            if tag:
                outbound_tags.add(tag)
                proxy_settings = outbound.get("proxySettings")
                if isinstance(proxy_settings, dict):
                    next_tag = str(proxy_settings.get("tag") or "").strip()
                    if next_tag:
                        proxy_graph[tag] = next_tag

        errors.extend(self._validate_proxy_cycles(proxy_graph))

        routing = payload.get("routing")
        if not isinstance(routing, dict):
            return errors + ["Upstream config must define routing"]

        balancers = routing.get("balancers") or []
        balancer_tags: set[str] = set()
        if balancers and not isinstance(balancers, list):
            errors.append("Routing balancers must be a list")
        elif isinstance(balancers, list):
            for index, balancer in enumerate(balancers):
                if not isinstance(balancer, dict):
                    errors.append(f"Balancer #{index + 1} must be an object")
                    continue
                tag = str(balancer.get("tag") or "").strip()
                if not tag:
                    errors.append(f"Balancer #{index + 1} has empty tag")
                    continue
                balancer_tags.add(tag)
                selector = balancer.get("selector")
                if not isinstance(selector, list) or not selector:
                    errors.append(f"Balancer '{tag}' must have a non-empty selector")
                    continue
                for selected in selector:
                    selected_tag = str(selected or "").strip()
                    if not selected_tag:
                        errors.append(f"Balancer '{tag}' has empty selector tag")
                    elif selected_tag not in outbound_tags:
                        errors.append(
                            f"Balancer '{tag}' references missing outboundTag '{selected_tag}'"
                        )

        rules = routing.get("rules")
        if not isinstance(rules, list) or not rules:
            errors.append("Routing rules must be a non-empty list")
        else:
            has_default_route = False
            for index, rule in enumerate(rules):
                if not isinstance(rule, dict):
                    errors.append(f"Routing rule #{index + 1} must be an object")
                    continue
                outbound_tag = str(rule.get("outboundTag") or "").strip()
                balancer_tag = str(rule.get("balancerTag") or "").strip()
                if outbound_tag:
                    if outbound_tag not in outbound_tags:
                        errors.append(
                            f"Routing rule #{index + 1} references missing outboundTag '{outbound_tag}'"
                        )
                elif balancer_tag:
                    if balancer_tag not in balancer_tags:
                        errors.append(
                            f"Routing rule #{index + 1} references missing balancerTag '{balancer_tag}'"
                        )
                else:
                    errors.append(f"Routing rule #{index + 1} must define outboundTag or balancerTag")
                if self._is_default_route_rule(rule):
                    has_default_route = True
            if not has_default_route:
                errors.append("Routing rules must contain an explicit default route")
        return errors

    def _validate_proxy_cycles(self, proxy_graph: dict[str, str]) -> list[str]:
        errors: list[str] = []
        visited: set[str] = set()
        for tag in proxy_graph:
            if tag in visited:
                continue
            path: list[str] = []
            seen_in_path: set[str] = set()
            current = tag
            while current in proxy_graph:
                if current in seen_in_path:
                    cycle = path[path.index(current):] + [current]
                    errors.append(f"Outbound proxy cycle detected: {' -> '.join(cycle)}")
                    break
                seen_in_path.add(current)
                visited.add(current)
                path.append(current)
                current = proxy_graph[current]
        return errors

    @staticmethod
    def _is_default_route_rule(rule: dict) -> bool:
        if not isinstance(rule, dict):
            return False
        if not str(rule.get("outboundTag") or rule.get("balancerTag") or "").strip():
            return False
        selectors = {
            "domain",
            "domains",
            "ip",
            "port",
            "source",
            "sourcePort",
            "user",
            "inboundTag",
            "protocol",
            "attrs",
            "app",
        }
        if any(rule.get(key) for key in selectors):
            return False
        network = str(rule.get("network") or "").strip().lower()
        return network in {"", "tcp,udp", "udp,tcp"}


class GatewayConfigRenderer:
    def render(self, server: ServerInfo, upstream_payload: dict) -> str:
        payload = dict(upstream_payload)
        meta = payload.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        meta.update(
            {
                "managed_by": "zybervpn",
                "server_id": server.id,
                "server_name": server.name,
            }
        )
        payload["meta"] = meta
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def build_effective_gateway_config(base_text: str, upstream_text: str) -> str:
    upstream_payload = json.loads(upstream_text)
    if not isinstance(upstream_payload, dict):
        raise RuntimeError("Gateway upstream payload must be a JSON object")
    if not base_text.strip():
        return json.dumps(upstream_payload, ensure_ascii=False, indent=2, sort_keys=True)

    base_payload = json.loads(base_text)
    if not isinstance(base_payload, dict):
        raise RuntimeError("Existing gateway config must be a JSON object")

    effective = deepcopy(base_payload)
    effective["meta"] = _merge_dict(
        _as_dict(base_payload.get("meta")),
        _as_dict(upstream_payload.get("meta")),
    )
    effective["log"] = _merge_dict(
        _as_dict(base_payload.get("log")),
        _as_dict(upstream_payload.get("log")),
    )
    effective["outbounds"] = _merge_outbounds(
        base_payload.get("outbounds"),
        upstream_payload.get("outbounds"),
    )
    effective["routing"] = _merge_routing(
        base_payload.get("routing"),
        upstream_payload.get("routing"),
    )

    for key in (
        "dns",
        "transport",
        "policy",
        "api",
        "stats",
        "reverse",
        "fakedns",
        "observatory",
        "burstObservatory",
        "metrics",
    ):
        if key not in effective and key in upstream_payload:
            effective[key] = deepcopy(upstream_payload[key])

    return json.dumps(effective, ensure_ascii=False, indent=2, sort_keys=True)


def _as_dict(value: object) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _merge_dict(base: dict, override: dict) -> dict:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _merge_outbounds(base_value: object, upstream_value: object) -> list[dict]:
    merged: list[dict] = []
    by_tag: dict[str, int] = {}
    for source in (base_value, upstream_value):
        if not isinstance(source, list):
            continue
        for outbound in source:
            if not isinstance(outbound, dict):
                continue
            item = deepcopy(outbound)
            tag = str(item.get("tag") or "").strip()
            if tag and tag in by_tag:
                merged[by_tag[tag]] = item
            else:
                if tag:
                    by_tag[tag] = len(merged)
                merged.append(item)
    return merged


def _merge_routing(base_value: object, upstream_value: object) -> dict:
    base = _as_dict(base_value)
    upstream = _as_dict(upstream_value)
    merged = deepcopy(base)
    if "domainStrategy" in upstream:
        merged["domainStrategy"] = upstream["domainStrategy"]

    merged_balancers: list[dict] = []
    balancer_positions: dict[str, int] = {}
    for source in (base.get("balancers"), upstream.get("balancers")):
        if not isinstance(source, list):
            continue
        for balancer in source:
            if not isinstance(balancer, dict):
                continue
            item = deepcopy(balancer)
            tag = str(item.get("tag") or "").strip()
            if tag and tag in balancer_positions:
                merged_balancers[balancer_positions[tag]] = item
            else:
                if tag:
                    balancer_positions[tag] = len(merged_balancers)
                merged_balancers.append(item)
    if merged_balancers:
        merged["balancers"] = merged_balancers

    merged_rules: list[dict] = []
    seen_rules: set[str] = set()
    for source in (base.get("rules"), upstream.get("rules")):
        if not isinstance(source, list):
            continue
        for rule in source:
            if not isinstance(rule, dict):
                continue
            item = deepcopy(rule)
            key = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if key in seen_rules:
                continue
            seen_rules.add(key)
            merged_rules.append(item)
    if merged_rules:
        merged["rules"] = merged_rules
    return merged
