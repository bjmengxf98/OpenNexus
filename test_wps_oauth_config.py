from urllib.parse import parse_qs, urlparse

from auth import wps_oauth


def test_explicit_wps_redirect_uri_has_highest_priority(monkeypatch):
    monkeypatch.setenv("WPS_REDIRECT_URI", "https://dept.example.com/oauth/callback")
    monkeypatch.setenv("APP_BASE_URL", "https://wrong.example.com")

    assert wps_oauth.get_redirect_uri() == "https://dept.example.com/oauth/callback"


def test_redirect_uri_falls_back_to_public_app_base_url(monkeypatch):
    monkeypatch.delenv("WPS_REDIRECT_URI", raising=False)
    monkeypatch.setenv("APP_BASE_URL", "https://dept.example.com/")

    assert wps_oauth.get_redirect_uri() == "https://dept.example.com/oauth/callback"


def test_authorization_url_uses_same_canonical_redirect_uri(monkeypatch):
    monkeypatch.setenv("WPS_REDIRECT_URI", "https://dept.example.com/oauth/callback")
    monkeypatch.setattr(wps_oauth, "get_app_id", lambda: "test-app-id")

    query = parse_qs(urlparse(wps_oauth.build_auth_url(7)).query)

    assert query["client_id"] == ["test-app-id"]
    assert query["redirect_uri"] == ["https://dept.example.com/oauth/callback"]
