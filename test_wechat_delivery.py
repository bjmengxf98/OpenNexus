import unittest
from pathlib import Path
from unittest.mock import patch

from core.wechat_delivery import (
    bridge_candidate_ports,
    deliver_personal_weixin,
    probe_personal_weixin_bridge,
)


class _FakeResponse:
    def __init__(self, status_code, data=None, text=""):
        self.status_code = status_code
        self._data = data or {}
        self.text = text

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, health_by_port, send_response=None):
        self.health_by_port = health_by_port
        self.send_response = send_response or _FakeResponse(200, {"ok": True})
        self.posts = []

    async def get(self, url, timeout=None):
        port = int(url.split(":")[2].split("/")[0])
        data = self.health_by_port.get(port)
        if data is None:
            raise OSError("port closed")
        return _FakeResponse(200, data)

    async def post(self, url, json=None):
        self.posts.append((url, json))
        return self.send_response


class WechatDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def test_candidate_ports_prefer_target_and_remove_duplicates(self):
        self.assertEqual(
            bridge_candidate_ports(
                "wx-b", {"wx-a": 3001, "wx-b": 3002}, (3001, 3002, 3003)
            ),
            [3002, 3001, 3003],
        )

    async def test_probe_recovers_empty_in_memory_port_map(self):
        port_map = {}
        client = _FakeClient({3002: {"ok": True, "userId": "wx-target"}})

        port, error = await probe_personal_weixin_bridge(
            "wx-target", port_map, fallback_ports=(3001, 3002), client=client
        )

        self.assertEqual((port, error), (3002, ""))
        self.assertEqual(port_map, {"wx-target": 3002})

    async def test_probe_rejects_a_different_online_account(self):
        client = _FakeClient({3001: {"ok": True, "userId": "wx-other"}})

        port, error = await probe_personal_weixin_bridge(
            "wx-target", {}, fallback_ports=(3001,), client=client
        )

        self.assertIsNone(port)
        self.assertIn("账号与当前绑定微信不一致", error)

    async def test_delivery_finds_bridge_without_saved_port_and_requires_ok(self):
        fake = _FakeClient({3002: {"ok": True, "userId": "wx-target"}})
        constructor_options = []

        class _OwnedClient:
            def __init__(self, *args, **kwargs):
                constructor_options.append(kwargs)

            async def __aenter__(self):
                return fake

            async def __aexit__(self, *args):
                return None

        with patch("core.wechat_delivery.httpx.AsyncClient", _OwnedClient):
            result = await deliver_personal_weixin(
                "wx-target", "测试提醒", "token", {},
                fallback_ports=(3001, 3002), attempts=1,
            )

        self.assertEqual(result, {"ok": True, "port": 3002})
        self.assertEqual(len(fake.posts), 1)
        self.assertEqual(fake.posts[0][1]["to"], "wx-target")
        self.assertFalse(constructor_options[0]["trust_env"])

    async def test_http_200_without_ok_is_not_reported_as_delivered(self):
        fake = _FakeClient(
            {3001: {"ok": True, "userId": "wx-target"}},
            _FakeResponse(200, {"ok": False, "error": "微信拒绝发送"}),
        )

        class _OwnedClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return fake

            async def __aexit__(self, *args):
                return None

        with patch("core.wechat_delivery.httpx.AsyncClient", _OwnedClient):
            result = await deliver_personal_weixin(
                "wx-target", "测试提醒", fallback_ports=(3001,), attempts=1
            )

        self.assertFalse(result["ok"])
        self.assertIn("微信拒绝发送", result["error"])


class WechatSettingsPageTests(unittest.TestCase):
    def test_settings_page_uses_real_connection_status_and_test_endpoint(self):
        page = Path("static/settings_new.html").read_text(encoding="utf-8")
        self.assertIn("/api/weixin/status", page)
        self.assertIn("/api/weixin/test", page)
        self.assertIn("发送测试消息", page)
        self.assertNotIn("d.personal_weixin_id?'当前状态：✅ 已绑定", page)


if __name__ == "__main__":
    unittest.main()
