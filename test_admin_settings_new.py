from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from api.admin_new_routes import admin_new_router
from api.settings_new_routes import settings_new_router


def _client(monkeypatch, *, admin=True):
    from api import admin_new_routes, settings_new_routes

    user = {
        "id": 1, "username": "tester", "is_enabled": 1,
        "is_admin": 1 if admin else 0, "role": "admin" if admin else "staff",
        "display_name": "测试用户", "wecom_userid": "",
    }
    monkeypatch.setattr(admin_new_routes.db, "get_user_by_id", lambda uid: user)
    monkeypatch.setattr(settings_new_routes.db, "get_user_by_id", lambda uid: user)
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(admin_new_router)
    app.include_router(settings_new_router)

    @app.get("/_login")
    async def login(request: Request):
        request.session["uid"] = 1
        return {"ok": True}

    client = TestClient(app)
    client.get("/_login")
    return client


def test_all_pages_have_no_server_ui_runtime():
    project_root = Path(__file__).parent
    root = project_root / "static"
    for name in ("auth.html", "app_new.html", "settings_new.html", "admin_new.html", "dashboard.html"):
        text = (root / name).read_text(encoding="utf-8")
        assert "_nicegui" not in text.lower()
        assert "quasar" not in text.lower()

    requirements = (project_root / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "nicegui" not in requirements
    assert not (project_root / "ui").exists()

    settings_html = (root / "settings_new.html").read_text(encoding="utf-8")
    assert 'id="mobileSection"' in settings_html
    assert '<option value="display">显示设置</option>' in settings_html

    runtime_files = [project_root / "app.py"]
    for folder in ("agent", "api", "auth", "core", "scripts"):
        runtime_files.extend((project_root / folder).rglob("*.py"))
    for path in runtime_files:
        text = path.read_text(encoding="utf-8").lower()
        assert "from nicegui" not in text, path
        assert "import nicegui" not in text, path
        assert "ui.run(" not in text, path


def test_admin_page_requires_admin(monkeypatch):
    client = _client(monkeypatch, admin=False)
    response = client.get("/admin-new", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"


def test_admin_bootstrap(monkeypatch):
    from api import admin_new_routes

    client = _client(monkeypatch)
    monkeypatch.setattr(admin_new_routes.db, "list_users", lambda: [
        {"id": 1, "is_enabled": 1, "is_admin": 1},
        {"id": 2, "is_enabled": 0, "is_admin": 0},
    ])
    monkeypatch.setattr(admin_new_routes.db, "list_feedback", lambda: [
        {"id": 1, "status": "pending"}, {"id": 2, "status": "done"},
    ])
    monkeypatch.setattr(admin_new_routes, "_knowledge_rows", lambda: [])
    monkeypatch.setattr(admin_new_routes.db, "get_embed_config", lambda: None)
    monkeypatch.setattr(admin_new_routes.db, "get_change_log", lambda **kwargs: [])
    monkeypatch.setattr(admin_new_routes.db, "get_system_config", lambda key, default=None: default)
    response = client.get("/api/admin-new/bootstrap")
    assert response.status_code == 200
    assert response.json()["stats"] == {
        "users": 2, "enabled": 1, "admins": 1, "pending_feedback": 1,
    }


def test_admin_cannot_disable_self(monkeypatch):
    client = _client(monkeypatch)
    response = client.patch("/api/admin-new/users/1/enabled", json={"enabled": False})
    assert response.status_code == 400
    assert "当前登录账号" in response.json()["error"]
