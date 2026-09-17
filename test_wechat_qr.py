from pathlib import Path


ROOT = Path(__file__).parent


def test_headless_linux_bridge_generates_web_qr_png():
    source = (ROOT / "wechat-claude-code-main" / "src" / "main.ts").read_text(encoding="utf-8")
    built = (ROOT / "wechat-claude-code-main" / "dist" / "main.js").read_text(encoding="utf-8")
    for text in (source, built):
        headless = text.index("if (isHeadlessLinux)")
        fallback = text.index("qrcode-terminal", headless)
        png_write = text.index("writeFileSync(QR_PATH, pngData)", headless)
        assert headless < png_write < fallback


def test_setup_process_exposes_errors_and_has_qr_timeout():
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "stderr=subprocess.STDOUT" in app
    assert "time.monotonic() - _wechat_setup_started_at > 45" in app
    assert "服务器未安装 Node.js" in app
    assert "[wx_setup]" in app

def test_bridge_uses_deployment_scoped_storage_and_identity():
    main = (ROOT / "wechat-claude-code-main" / "src" / "main.ts").read_text(encoding="utf-8")
    accounts = (ROOT / "wechat-claude-code-main" / "src" / "wechat" / "accounts.ts").read_text(encoding="utf-8")
    config = (ROOT / "wechat-claude-code-main" / "src" / "config.ts").read_text(encoding="utf-8")
    logger = (ROOT / "wechat-claude-code-main" / "src" / "logger.ts").read_text(encoding="utf-8")
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    assistant = (ROOT / "agent" / "assistant.py").read_text(encoding="utf-8")
    mcp = (ROOT / "core" / "mcp_server.py").read_text(encoding="utf-8")

    assert "OPENNEXUS_WECHAT_INSTANCE_ID" in main
    assert "instanceId" in main
    assert "{ maxRetries: 0 }" in main
    assert "DATA_DIR" in accounts
    assert "DATA_DIR" in config
    assert "DATA_DIR" in logger
    assert 'Path.home() / ".wechat-claude-code"' not in app
    assert "range(3001, 3011)" not in app
    assert "range(3001, 3011)" not in assistant
    assert "range(3001, 3011)" not in mcp
    assert "_wechat_revoked_ids.add(weixin_id)" in app
    assert "secrets.compare_digest" in app
    assert "safe_account_id" in app
    assert "account_file.unlink()" in app


def test_bridge_requires_and_persists_conversation_context_for_active_send():
    main = (
        ROOT / "wechat-claude-code-main" / "src" / "main.ts"
    ).read_text(encoding="utf-8")
    state = (
        ROOT / "wechat-claude-code-main" / "src" / "wechat" / "outbound-state.ts"
    ).read_text(encoding="utf-8")

    assert "outboundStore.getContext(to)" in main
    assert "outboundStore.rememberContext(fromUserId, contextToken)" in main
    assert "sender.sendText(to, '', text" not in main
    assert "outboundStore.clearContext(to)" not in main
    assert "outboundStore.clearContext(fromUserId)" not in main
    assert "wechat_activation_required" in main
    assert "code: 'wechat_send_deferred'" in main
    assert "系统不会清除已保存的会话" in main
    assert "微信会话上下文已失效" not in main
    assert "queueIfInactive" in main
    assert "DEFAULT_PENDING_TTL_MS = 30 * 60 * 1000" in state
