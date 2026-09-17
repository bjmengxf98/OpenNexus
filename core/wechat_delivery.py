"""Reliable delivery helpers for the local personal-WeChat bridge."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any

import httpx


# An empty fallback is intentional. A deployment may use only bridge ports it
# owns; scanning the historical global range can silently borrow another local
# OpenNexus checkout's WeChat session.
DEFAULT_BRIDGE_PORTS: tuple[int, ...] = ()


def bridge_candidate_ports(
    target_weixin_id: str,
    port_map: Mapping[str, int] | None = None,
    fallback_ports: Sequence[int] = DEFAULT_BRIDGE_PORTS,
) -> list[int]:
    """Return de-duplicated bridge ports, preferring the target's known port."""
    result: list[int] = []
    if port_map:
        mapped = port_map.get(target_weixin_id)
        if mapped:
            result.append(int(mapped))
        result.extend(int(port) for port in port_map.values() if port)
    result.extend(int(port) for port in fallback_ports if port)
    return list(dict.fromkeys(result))


async def _health_for_port(
    client: httpx.AsyncClient,
    port: int,
) -> tuple[int, dict[str, Any] | None]:
    try:
        response = await client.get(
            f"http://127.0.0.1:{port}/health",
            timeout=1.0,
        )
        if response.status_code != 200:
            return port, None
        data = response.json()
        return port, data if isinstance(data, dict) else None
    except Exception:
        return port, None


async def _probe_details_with_client(
    target_weixin_id: str,
    port_map: Mapping[str, int] | None,
    fallback_ports: Sequence[int],
    client: httpx.AsyncClient,
    expected_instance_id: str = "",
) -> tuple[int | None, str, dict[str, Any] | None]:
    ports = bridge_candidate_ports(target_weixin_id, port_map, fallback_ports)
    checks = await asyncio.gather(
        *(_health_for_port(client, port) for port in ports),
    )
    online_accounts: list[str] = []
    foreign_instance_seen = False
    for port, data in checks:
        if not data or data.get("ok") is not True:
            continue
        if expected_instance_id and data.get("instanceId") != expected_instance_id:
            foreign_instance_seen = True
            continue
        bridge_user_id = str(data.get("userId") or "")
        if bridge_user_id:
            online_accounts.append(bridge_user_id)
        if bridge_user_id == target_weixin_id:
            if isinstance(port_map, MutableMapping):
                port_map[target_weixin_id] = port
            return port, "", data

    if online_accounts:
        return None, "检测到微信桥接，但桥接账号与当前绑定微信不一致", None
    if foreign_instance_seen:
        return (
            None,
            "检测到其他 OpenNexus 实例的微信桥接；请在本系统重新扫码连接",
            None,
        )
    return None, "微信桥接未运行；请在设置页重新扫码绑定", None


async def _probe_with_client(
    target_weixin_id: str,
    port_map: Mapping[str, int] | None,
    fallback_ports: Sequence[int],
    client: httpx.AsyncClient,
    expected_instance_id: str = "",
) -> tuple[int | None, str]:
    port, error, _ = await _probe_details_with_client(
        target_weixin_id,
        port_map,
        fallback_ports,
        client,
        expected_instance_id,
    )
    return port, error


async def probe_personal_weixin_bridge(
    target_weixin_id: str,
    port_map: Mapping[str, int] | None = None,
    *,
    fallback_ports: Sequence[int] = DEFAULT_BRIDGE_PORTS,
    client: httpx.AsyncClient | None = None,
    expected_instance_id: str = "",
) -> tuple[int | None, str]:
    """Find the live bridge that loaded exactly ``target_weixin_id``."""
    target_weixin_id = (target_weixin_id or "").strip()
    if not target_weixin_id:
        return None, "尚未绑定个人微信"
    if client is not None:
        return await _probe_with_client(
            target_weixin_id, port_map, fallback_ports, client,
            expected_instance_id,
        )
    async with httpx.AsyncClient(timeout=3, trust_env=False) as owned_client:
        return await _probe_with_client(
            target_weixin_id, port_map, fallback_ports, owned_client,
            expected_instance_id,
        )


async def inspect_personal_weixin_bridge(
    target_weixin_id: str,
    port_map: Mapping[str, int] | None = None,
    *,
    fallback_ports: Sequence[int] = DEFAULT_BRIDGE_PORTS,
    client: httpx.AsyncClient | None = None,
    expected_instance_id: str = "",
) -> dict[str, Any]:
    """Return connection and conversation-activation state without secrets."""
    target_weixin_id = (target_weixin_id or "").strip()
    if not target_weixin_id:
        return {
            "connected": False,
            "activated": False,
            "port": None,
            "pending_count": 0,
            "error": "尚未绑定个人微信",
        }

    async def _inspect(active_client: httpx.AsyncClient) -> dict[str, Any]:
        port, error, health = await _probe_details_with_client(
            target_weixin_id,
            port_map,
            fallback_ports,
            active_client,
            expected_instance_id,
        )
        return {
            "connected": port is not None,
            "activated": bool((health or {}).get("activated")),
            "port": port,
            "pending_count": int((health or {}).get("pendingCount") or 0),
            "context_updated_at": (health or {}).get("contextUpdatedAt"),
            "error": error,
        }

    if client is not None:
        return await _inspect(client)
    async with httpx.AsyncClient(timeout=3, trust_env=False) as owned_client:
        return await _inspect(owned_client)


async def deliver_personal_weixin(
    target_weixin_id: str,
    text: str,
    token: str = "",
    port_map: MutableMapping[str, int] | None = None,
    *,
    fallback_ports: Sequence[int] = DEFAULT_BRIDGE_PORTS,
    attempts: int = 3,
    expected_instance_id: str = "",
    queue_if_inactive: bool = False,
) -> dict[str, Any]:
    """Deliver text through the exact user's bridge and confirm ``ok=true``."""
    target_weixin_id = (target_weixin_id or "").strip()
    text = (text or "").strip()
    if not target_weixin_id:
        return {"ok": False, "error": "尚未绑定个人微信"}
    if not text:
        return {"ok": False, "error": "消息内容为空"}

    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        port, probe_error = await _probe_with_client(
            target_weixin_id, port_map, fallback_ports, client,
            expected_instance_id,
        )
        if port is None:
            return {"ok": False, "error": probe_error}

        last_result: dict[str, Any] = {
            "ok": False,
            "port": port,
            "error": "微信桥接未返回发送结果",
        }
        for attempt in range(max(1, attempts)):
            try:
                response = await client.post(
                    f"http://127.0.0.1:{port}/local/send",
                    json={
                        "to": target_weixin_id,
                        "text": text,
                        "token": token or "",
                        "queueIfInactive": bool(queue_if_inactive),
                    },
                )
                try:
                    data = response.json()
                except Exception:
                    data = {}
                if response.status_code == 200 and data.get("ok") is True:
                    result: dict[str, Any] = {"ok": True, "port": port}
                    if data.get("messageId"):
                        result["message_id"] = data["messageId"]
                    return result
                detail = str(data.get("error") or response.text or "桥接返回空错误").strip()
                last_result = {
                    "ok": False,
                    "port": port,
                    "status_code": response.status_code,
                    "error": detail,
                }
                for key in ("code", "queued"):
                    if key in data:
                        last_result[key] = data[key]
                if response.status_code in {202, 400, 401, 403, 409}:
                    break
            except Exception as exc:
                last_result = {
                    "ok": False,
                    "port": port,
                    "error": f"连接微信桥接失败：{exc}",
                }
            if attempt + 1 < max(1, attempts):
                await asyncio.sleep(1)
        return last_result
