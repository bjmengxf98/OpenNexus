"""HTTP contracts for token preauthorization, using only temporary databases."""
import ast
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from auth import db
from core import tool_governance as governance


def _users(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "mcp-http.db")
    db.init_db()
    for name in ("client-a", "client-b"):
        assert db.create_user(name, f"{name}@example.test", "test-password")[0]
    return [db.get_user_by_username(name)["id"] for name in ("client-a", "client-b")]


def _token_api_client():
    # app.py has import-time database initialization and legacy cleanup.
    # Compile its actual token handlers without executing those startup side effects.
    path = Path(__file__).parent / "app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {"get_current_user", "api_list_mcp_tokens", "api_create_mcp_token", "api_revoke_mcp_token"}
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    assert len(nodes) == len(names)
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    namespace = {"Request": Request, "JSONResponse": JSONResponse, "db": db,
                 "fastapi_app": app, "normalize_requested_scopes": governance.normalize_requested_scopes}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)

    @app.get("/_login/{uid}")
    async def login(uid: int, request: Request):
        request.session["uid"] = uid
        return {"ok": True}

    return TestClient(app)


def test_token_creation_requires_explicit_extra_permissions_and_remains_owner_scoped(monkeypatch, tmp_path):
    uid_a, uid_b = _users(monkeypatch, tmp_path)
    client = _token_api_client()
    assert client.post("/api/mcp/tokens", json={"scopes": ["all"]}).status_code == 401
    client.get(f"/_login/{uid_a}")
    default = client.post("/api/mcp/tokens", json={"name": "default-client"}).json()
    assert default["scopes"] == ["read"]
    permitted = client.post("/api/mcp/tokens", json={
        "name": "trusted-client", "scopes": ["read", "send_messages", "delete_data"], "expires_days": 30,
    }).json()
    assert permitted["scopes"] == ["read", "send_messages", "delete_data"]
    assert permitted["expires_at"]
    before = len(db.list_mcp_tokens(uid_a))
    for scopes in ([], ["unknown"], ["all", "unknown"]):
        assert client.post("/api/mcp/tokens", json={"scopes": scopes}).status_code == 400
    assert len(db.list_mcp_tokens(uid_a)) == before
    listed = client.get("/api/mcp/tokens").json()["tokens"]
    assert all("token" not in row and "token_hash" not in row for row in listed)

    client.get(f"/_login/{uid_b}")
    assert client.get("/api/mcp/tokens").json()["tokens"] == []
    assert client.delete(f"/api/mcp/tokens/{permitted['id']}").status_code == 404
    assert db.verify_mcp_token(permitted["token"]) is not None
    client.get(f"/_login/{uid_a}")
    assert client.delete(f"/api/mcp/tokens/{permitted['id']}").status_code == 200
    assert db.verify_mcp_token(permitted["token"]) is None


def test_mcp_http_middleware_keeps_identity_isolated_and_rejects_revoked_token(monkeypatch, tmp_path):
    uid_a, uid_b = _users(monkeypatch, tmp_path)
    from core import mcp_server as service
    token_a = db.create_mcp_token(uid_a, scopes=["read"])
    token_b = db.create_mcp_token(uid_b, scopes=["read"])
    seen = []

    async def handler(scope, receive, send):
        result = await service.execute_tool("get_current_user", {})
        seen.append(result["user"]["id"])
        await JSONResponse(result)(scope, receive, send)

    client = TestClient(service.MCPBearerMiddleware(handler))
    for token, uid in ((token_a, uid_a), (token_b, uid_b)):
        response = client.post("/", headers={"Authorization": "Bearer " + token["token"]})
        assert response.status_code == 200
        assert response.json()["user"]["id"] == uid
    assert seen == [uid_a, uid_b]
    assert service._identity_var.get() is None
    assert client.post("/").status_code == 401
    assert db.revoke_mcp_token(uid_a, token_a["id"])
    assert client.post("/", headers={"Authorization": "Bearer " + token_a["token"]}).status_code == 401
    assert seen == [uid_a, uid_b]
