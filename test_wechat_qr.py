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
