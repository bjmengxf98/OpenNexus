from pathlib import Path

from PIL import Image


ROOT = Path(__file__).parent
STATIC = ROOT / "static"
PAGES = ("auth.html", "app_new.html", "settings_new.html", "dashboard.html", "admin_new.html")


def test_every_primary_page_loads_shared_pwa_runtime():
    for name in PAGES:
        html = (STATIC / name).read_text(encoding="utf-8")
        assert 'rel="manifest" href="/manifest.json"' in html, name
        assert 'src="/static/pwa.js?v=20260821-1"' in html, name
        assert 'rel="apple-touch-icon"' in html, name
        assert "viewport-fit=cover" in html, name


def test_service_worker_has_safe_offline_and_update_flow():
    worker = (STATIC / "service-worker.js").read_text(encoding="utf-8")
    runtime = (STATIC / "pwa.js").read_text(encoding="utf-8")
    assert "'/offline'" in worker
    assert "request.mode === 'navigate'" in worker
    assert "url.pathname.startsWith('/api/')" in worker
    assert "SKIP_WAITING" in worker
    assert "navigator.serviceWorker.register('/sw.js'" in runtime
    assert "beforeinstallprompt" in runtime
    assert "isMobileDevice" in runtime
    assert "localStorage.getItem(INSTALL_DISMISS_KEY)" in runtime
    assert "INSTALL_DISMISS_MS = 30 * 24 * 60 * 60 * 1000" in runtime
    assert "sessionStorage.getItem('pwa-install-dismissed')" not in runtime
    assert "controllerchange" in runtime
    assert "checkForUpdate" in runtime
    assert "opennexus-pwa-v4" in worker
    assert "'/static/pwa.js?v=20260821-1'" in worker
    assert ".then(() => self.skipWaiting())" in worker


def test_manifest_and_mobile_navigation_are_complete():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    app_html = (STATIC / "app_new.html").read_text(encoding="utf-8")
    assert '"orientation": "any"' in source
    assert '"scope": "/"' in source
    assert '"purpose": "maskable"' in source
    assert 'id="mobileHelpBtn"' in app_html
    assert 'id="mobileUpdateBtn"' in app_html
    assert 'class="mobile-nav"' in app_html
    assert "--app-height" in app_html
    assert "visualViewport" in app_html
    assert "overscroll-behavior:none" in app_html
    assert 'html[data-theme="dark"] .help-body code' in app_html


def test_pwa_icons_have_expected_dimensions():
    expected = {
        "icon-192.png": (192, 192),
        "icon-512.png": (512, 512),
        "icon-maskable-512.png": (512, 512),
        "apple-touch-icon.png": (180, 180),
    }
    for name, size in expected.items():
        with Image.open(STATIC / name) as image:
            assert image.size == size
