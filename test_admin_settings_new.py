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
    assert "添加自定义模型" in settings_html
    assert "model_profiles" in settings_html
    assert "deleteModelProfile" in settings_html
    assert "模型 ID（可输入或从列表选择）" in settings_html
    assert "<datalist" in settings_html
    assert "高级配置（工具、多模态、推理与上下文）" in settings_html
    assert 'data-advanced="supports_tools"' in settings_html
    assert 'data-advanced="supports_vision"' in settings_html
    assert 'data-advanced="reasoning_mode"' in settings_html
    assert 'data-advanced="context_window"' in settings_html
    assert 'id="tokenScopes"' in settings_html
    assert 'id="approvalList"' in settings_html
    assert "selectedScopes()" in settings_html
    assert "/api/settings/mcp/approvals/" in settings_html

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


def test_mcp_approval_api_is_user_scoped(monkeypatch):
    from api import settings_new_routes

    client = _client(monkeypatch)
    approvals = [{
        "id": "onx_apr_test", "status": "pending", "summary": "删除记录",
    }]
    decisions = []
    monkeypatch.setattr(
        settings_new_routes.db, "list_mcp_tool_approvals", lambda uid: approvals,
    )
    monkeypatch.setattr(
        settings_new_routes.db,
        "decide_mcp_tool_approval",
        lambda uid, approval_id, decision: decisions.append(
            (uid, approval_id, decision),
        ) or {**approvals[0], "status": decision},
    )

    response = client.get("/api/settings/mcp/approvals")
    assert response.status_code == 200
    assert response.json()["approvals"] == approvals

    response = client.post(
        "/api/settings/mcp/approvals/onx_apr_test/decision",
        json={"decision": "approved"},
    )
    assert response.status_code == 200
    assert decisions == [(1, "onx_apr_test", "approved")]

    response = client.post(
        "/api/settings/mcp/approvals/onx_apr_test/decision",
        json={"decision": "invalid"},
    )
    assert response.status_code == 400


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


def test_custom_openai_provider_can_be_saved(monkeypatch):
    from api import settings_new_routes

    client = _client(monkeypatch)
    saved = {}

    def capture(*values):
        saved["values"] = values

    monkeypatch.setattr(settings_new_routes.db, "save_llm_key", capture)
    response = client.post("/api/settings/models/main", json={
        "provider": "custom_openai",
        "api_key": "test-key",
        "base_url": "https://models.example.com/v1/chat/completions",
        "model": "example-vision-2.5",
        "advanced": {
            "supports_tools": True,
            "supports_vision": True,
            "reasoning_mode": "on",
            "reasoning_effort": "high",
            "context_window": 128000,
            "max_output_tokens": 16000,
        },
    })

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["provider_id"].startswith("custom_openai:")
    assert saved["values"] == (
        1,
        payload["provider_id"],
        "test-key",
        "https://models.example.com/v1",
        "example-vision-2.5",
        {
            "supports_tools": True,
            "supports_vision": True,
            "reasoning_mode": "on",
            "reasoning_effort": "high",
            "context_window": 128000,
            "max_output_tokens": 16000,
        },
    )


def test_multiple_custom_models_get_independent_profile_ids(monkeypatch):
    from api import settings_new_routes

    client = _client(monkeypatch)
    saved = []
    monkeypatch.setattr(settings_new_routes.db, "save_llm_key", lambda *values: saved.append(values))

    first = client.post("/api/settings/models/main", json={
        "provider": "custom_openai",
        "api_key": "key-one",
        "base_url": "https://one.example.com/v1",
        "model": "model-one",
    })
    second = client.post("/api/settings/models/main", json={
        "provider": "custom_openai",
        "api_key": "key-two",
        "base_url": "https://two.example.com/v1",
        "model": "model-two",
    })

    assert first.status_code == second.status_code == 200
    first_id = first.json()["provider_id"]
    second_id = second.json()["provider_id"]
    assert first_id.startswith("custom_openai:")
    assert second_id.startswith("custom_openai:")
    assert first_id != second_id
    assert [row[1] for row in saved] == [first_id, second_id]


def test_custom_model_profile_can_be_deleted_only_when_inactive(monkeypatch):
    from api import settings_new_routes

    client = _client(monkeypatch)
    provider = "custom_openai:profile123"
    deleted = []
    monkeypatch.setattr(settings_new_routes.db, "get_llm_key", lambda uid: {"provider": "agnes"})
    monkeypatch.setattr(
        settings_new_routes.db,
        "delete_custom_provider_config",
        lambda uid, value, image=False: deleted.append((uid, value, image)),
    )
    monkeypatch.setattr(settings_new_routes.db, "list_custom_provider_configs", lambda uid, image=False: [])

    response = client.delete(f"/api/settings/provider/main/{provider}")
    assert response.status_code == 200
    assert deleted == [(1, provider, False)]

    monkeypatch.setattr(settings_new_routes.db, "get_llm_key", lambda uid: {"provider": provider})
    response = client.delete(f"/api/settings/provider/main/{provider}")
    assert response.status_code == 409
    assert "正在使用" in response.json()["error"]


def test_custom_openai_provider_validates_required_fields(monkeypatch):
    client = _client(monkeypatch)

    response = client.post("/api/settings/models/image", json={
        "provider": "custom_openai",
        "api_key": "test-key",
        "base_url": "not-a-url",
        "model": "vision-model",
    })

    assert response.status_code == 400
    assert "Base URL" in response.json()["error"]


def test_preset_provider_accepts_manually_entered_model_id(monkeypatch):
    from api import settings_new_routes

    client = _client(monkeypatch)
    saved = {}
    monkeypatch.setattr(
        settings_new_routes.db,
        "save_llm_key",
        lambda *values: saved.setdefault("values", values),
    )
    response = client.post("/api/settings/models/main", json={
        "provider": "agnes",
        "api_key": "test-key",
        "base_url": "https://apihub.agnes-ai.com/v1",
        "model": "agnes-2.5-flash",
    })

    assert response.status_code == 200
    assert saved["values"][-2] == "agnes-2.5-flash"
    assert saved["values"][-1]["supports_tools"] is True


def test_advanced_model_config_validates_token_limits(monkeypatch):
    client = _client(monkeypatch)
    response = client.post("/api/settings/models/main", json={
        "provider": "agnes",
        "api_key": "test-key",
        "model": "agnes-2.5-flash",
        "advanced": {"context_window": 100, "max_output_tokens": 8192},
    })
    assert response.status_code == 400
    assert "context_window" in response.json()["error"]


def test_model_advanced_config_round_trips_in_database(monkeypatch, tmp_path):
    from auth import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "model-config.db")
    db.init_db()
    main_advanced = {
        "supports_tools": True,
        "supports_vision": True,
        "reasoning_mode": "auto",
        "reasoning_effort": "medium",
        "context_window": 256000,
        "max_output_tokens": 32000,
    }
    image_advanced = {
        "supports_tools": False,
        "supports_vision": True,
        "reasoning_mode": "off",
        "reasoning_effort": "auto",
        "context_window": None,
        "max_output_tokens": 4000,
    }
    db.save_llm_key(99, "custom_openai:main-one", "main-key", "https://example.com/v1", "omni-2.5", main_advanced)
    db.save_llm_key(99, "custom_openai:main-two", "main-key-2", "https://example.net/v1", "omni-3", main_advanced)
    db.save_image_llm_key(99, "custom_openai:image-one", "image-key", "https://example.com/v1", "vision-1", image_advanced)

    assert db.get_llm_key(99)["advanced"] == main_advanced
    assert db.get_provider_config(99, "custom_openai:main-one")["advanced"] == main_advanced
    assert db.get_image_llm_key(99)["advanced"] == image_advanced
    assert db.get_image_provider_config(99, "custom_openai:image-one")["advanced"] == image_advanced
    assert {row["provider"] for row in db.list_custom_provider_configs(99)} == {
        "custom_openai:main-one", "custom_openai:main-two",
    }
    assert [row["provider"] for row in db.list_custom_provider_configs(99, image=True)] == [
        "custom_openai:image-one",
    ]


def test_assistant_uses_declared_model_capabilities():
    from agent.assistant import Assistant

    assistant = Assistant(
        "test-key", "custom_openai", "https://example.com/v1", "omni-2.5",
        advanced={
            "supports_tools": False,
            "supports_vision": True,
            "reasoning_mode": "on",
            "reasoning_effort": "medium",
            "context_window": 128000,
            "max_output_tokens": 12000,
        },
    )
    assert assistant.supports_tools is False
    assert assistant.supports_vision is True
    assert assistant.reasoning_mode == "on"
    assert assistant.reasoning_effort == "medium"
    assert assistant.context_window == 128000
    assert assistant.max_output_tokens == 12000


def test_image_parser_uses_configured_output_limit(monkeypatch, tmp_path):
    import httpx
    from core.file_parser import parse_file

    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "识别成功"}}]}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs["json"]
        return Response()

    monkeypatch.setattr(httpx, "post", fake_post)
    image = tmp_path / "sample.png"
    image.write_bytes(b"not-a-real-image-but-base64-is-enough")
    result = parse_file(
        str(image), image.name, "test-key", "https://example.com/v1", "omni-2.5", 6000,
    )

    assert result == "识别成功"
    assert captured["url"] == "https://example.com/v1/chat/completions"
    assert captured["json"]["model"] == "omni-2.5"
    assert captured["json"]["max_tokens"] == 6000
