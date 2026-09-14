"""Current OpenNexus product name stays consistent across user-facing surfaces."""

from pathlib import Path

from auth import email_sender


ROOT = Path(__file__).parent
PRODUCT = "OpenNexus 业务智能助手"
OLD_NAME = "多维表格智能助手"


def test_current_pages_use_business_assistant_name():
    static = ROOT / "static"
    auth = (static / "auth.html").read_text(encoding="utf-8")
    app = (static / "app_new.html").read_text(encoding="utf-8")
    offline = (static / "offline.html").read_text(encoding="utf-8")

    assert f"<title>{PRODUCT}</title>" in auth
    assert "login:['业务智能助手'" in auth
    assert "加入业务智能助手平台" in auth
    assert f"<title>{PRODUCT}</title>" in app
    assert '<span class="brand-sub">业务智能助手</span>' in app
    assert "我是你的业务智能助手" in app
    assert "业务智能助手</div>" in offline

    for page in (
        "auth.html", "app_new.html", "offline.html", "settings_new.html",
        "admin_new.html", "dashboard.html",
    ):
        text = (static / page).read_text(encoding="utf-8")
        assert OLD_NAME not in text, page
        assert PRODUCT in text or "业务智能助手" in text, page

    # WPS 多维表格 is a capability name, not the product name.
    assert "WPS 多维表格" in (static / "settings_new.html").read_text(encoding="utf-8")


def test_pwa_and_mcp_display_names_are_consistent():
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    mcp = (ROOT / "core" / "mcp_server.py").read_text(encoding="utf-8")
    assert app.count(f'"name": "{PRODUCT}"') == 2  # PWA manifest and MCP info
    assert f'"{PRODUCT}"' in mcp
    assert "我是你的业务智能助手" in app
    assert OLD_NAME not in app
    assert "opennexus-pwa-v5" in (ROOT / "static" / "service-worker.js").read_text(encoding="utf-8")


def test_authentication_emails_use_new_brand(monkeypatch):
    sent = []

    def capture(to_email, subject, html):
        sent.append((to_email, subject, html))
        return True, "发送成功"

    monkeypatch.setattr(email_sender, "send_email", capture)
    assert email_sender.SENDER_NAME == PRODUCT
    email_sender.send_verify_email("someone@example.com", "测试用户", "verify-token")
    email_sender.send_reset_email("someone@example.com", "测试用户", "reset-token")
    assert [subject for _, subject, _ in sent] == [
        f"【{PRODUCT}】邮箱验证", f"【{PRODUCT}】密码重置",
    ]
    assert all(PRODUCT in html and OLD_NAME not in html for _, _, html in sent)


def test_current_help_and_readme_use_new_name():
    for relative in (
        "README.md", "docs/用户帮助.md",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert PRODUCT in text, relative
        assert OLD_NAME not in text, relative
