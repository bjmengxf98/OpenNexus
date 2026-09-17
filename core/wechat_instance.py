"""Deployment-scoped settings for the optional personal-WeChat bridge."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


def _safe_instance_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-.")
    return cleaned[:64] or "opennexus"


@dataclass(frozen=True)
class WechatInstanceSettings:
    instance_id: str
    data_root: Path
    port_base: int
    port_count: int
    internal_url: str

    @property
    def accounts_dir(self) -> Path:
        return self.data_root / "accounts"

    @property
    def qr_path(self) -> Path:
        return self.data_root / "qrcode.png"

    @property
    def ports(self) -> tuple[int, ...]:
        return tuple(range(self.port_base, self.port_base + self.port_count))

    def child_env(
        self,
        base_env: Mapping[str, str] | None = None,
        *,
        api_token: str = "",
    ) -> dict[str, str]:
        env = dict(base_env if base_env is not None else os.environ)
        api_url = str(
            env.get("OPENNEXUS_INTERNAL_URL") or self.internal_url
        ).strip() or self.internal_url
        env.update({
            "WCC_DATA_DIR": str(self.data_root),
            "OPENNEXUS_WECHAT_INSTANCE_ID": self.instance_id,
            "WCC_API_URL": api_url.rstrip("/"),
            "WCC_API_TOKEN": api_token or "",
        })
        return env


def resolve_wechat_instance_settings(
    app_dir: Path,
    environ: Mapping[str, str] | None = None,
) -> WechatInstanceSettings:
    """Resolve isolated paths and ports for one OpenNexus deployment.

    The path-derived defaults keep sibling checkouts on the same computer from
    sharing credentials or bridge ports. Deployments may set explicit values to
    keep their identity stable after moving the source directory.
    """
    env = environ if environ is not None else os.environ
    resolved_app = str(Path(app_dir).resolve()).casefold()
    digest = hashlib.sha256(resolved_app.encode("utf-8")).hexdigest()
    instance_id = _safe_instance_id(
        env.get("OPENNEXUS_WECHAT_INSTANCE_ID", "") or f"opennexus-{digest[:12]}"
    )

    configured_root = str(env.get("OPENNEXUS_WECHAT_DATA_DIR", "") or "").strip()
    data_root = (
        Path(configured_root).expanduser()
        if configured_root
        else Path.home() / ".opennexus-wechat" / instance_id
    )

    raw_base = str(env.get("OPENNEXUS_WECHAT_PORT_BASE", "") or "").strip()
    port_base = int(raw_base) if raw_base else 31000 + (int(digest[:8], 16) % 1900) * 10
    raw_count = str(env.get("OPENNEXUS_WECHAT_PORT_COUNT", "10") or "10").strip()
    port_count = int(raw_count)
    if port_base < 1024 or port_count < 1 or port_count > 50:
        raise ValueError("微信桥接端口配置无效")
    if port_base + port_count - 1 > 65535:
        raise ValueError("微信桥接端口范围超过 65535")

    internal_url = str(
        env.get("OPENNEXUS_INTERNAL_URL", "http://127.0.0.1:8000")
    ).strip() or "http://127.0.0.1:8000"
    return WechatInstanceSettings(
        instance_id=instance_id,
        data_root=data_root,
        port_base=port_base,
        port_count=port_count,
        internal_url=internal_url,
    )
