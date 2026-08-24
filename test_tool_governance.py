import asyncio
import json
import sqlite3

from auth import db
from core import tool_governance as governance


def _fresh_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "governance.db")
    db.init_db()


def test_scope_validation_and_policy_classification():
    assert governance.normalize_requested_scopes(None) == ["all"]
    assert governance.normalize_requested_scopes(["read", "write_records", "read"]) == [
        "read", "write_records",
    ]
    assert governance.granted_scopes(None) == set()

    assert governance.policy_for_tool("list_records").read_only is True
    assert governance.policy_for_tool("delete_records").destructive is True
    assert governance.policy_for_tool("send_weixin_message").risk == "external_message"
    assert governance.policy_for_tool(
        "update_records", {"records": [{"id": "1"}, {"id": "2"}]},
    ).approval_required is True
    assert governance.policy_for_tool(
        "update_records", {"records": [{"id": "1"}]},
    ).approval_required is False


def test_token_scopes_are_structured_and_legacy_default_is_all(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    legacy = db.create_mcp_token(7, "旧客户端")
    limited = db.create_mcp_token(7, "只读客户端", scopes=["read"])

    assert legacy["scopes"] == ["all"]
    assert limited["scopes"] == ["read"]
    rows = {row["id"]: row for row in db.list_mcp_tokens(7)}
    assert rows[legacy["id"]]["scopes"] == ["all"]
    assert rows[limited["id"]]["scopes"] == ["read"]


def test_old_database_gets_additive_governance_migration(monkeypatch, tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
        CREATE TABLE mcp_tokens (
            id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, token_hash TEXT,
            token_prefix TEXT, scopes TEXT, is_active INTEGER, created_at TEXT,
            last_used_at TEXT, expires_at TEXT
        );
        CREATE TABLE mcp_audit_log (
            id INTEGER PRIMARY KEY, user_id INTEGER, token_id INTEGER,
            tool_name TEXT, arguments TEXT, success INTEGER, error TEXT,
            duration_ms INTEGER, created_at TEXT
        );
        """)
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()

    with sqlite3.connect(path) as conn:
        audit_columns = {row[1] for row in conn.execute("PRAGMA table_info(mcp_audit_log)")}
        approval_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mcp_tool_approvals'",
        ).fetchone()
    assert {"risk", "decision", "approval_id", "required_scope"} <= audit_columns
    assert approval_table == ("mcp_tool_approvals",)


def test_approval_is_bound_to_token_tool_arguments_and_single_use(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    token = db.create_mcp_token(9, scopes=["all"])
    identity = {"user_id": 9, "id": token["id"], "scopes": ["all"]}
    original = {
        "file_id": "file-a", "sheet_id": 1, "record_ids": ["row-1"],
    }

    first = governance.new_tool_context(identity, "delete_records", original)
    decision = asyncio.run(governance.run_pre_tool_hooks(first))
    assert decision.action == "ask"
    approval_id = decision.payload["approval_id"]
    assert decision.payload["approval_required"] is True

    duplicate = governance.new_tool_context(identity, "delete_records", original)
    duplicate_decision = asyncio.run(governance.run_pre_tool_hooks(duplicate))
    assert duplicate_decision.payload["approval_id"] == approval_id

    assert db.decide_mcp_tool_approval(9, approval_id, "approved")["status"] == "approved"
    changed = governance.new_tool_context(
        identity,
        "delete_records",
        {**original, "record_ids": ["row-2"], "approval_id": approval_id},
    )
    changed_decision = asyncio.run(governance.run_pre_tool_hooks(changed))
    assert changed_decision.payload["code"] == "approval_invalid"
    assert db.get_mcp_tool_approval(9, approval_id)["status"] == "approved"

    approved = governance.new_tool_context(
        identity, "delete_records", {**original, "approval_id": approval_id},
    )
    assert asyncio.run(governance.run_pre_tool_hooks(approved)).action == "allow"
    assert db.get_mcp_tool_approval(9, approval_id)["status"] == "executed"

    replay = governance.new_tool_context(
        identity, "delete_records", {**original, "approval_id": approval_id},
    )
    assert asyncio.run(governance.run_pre_tool_hooks(replay)).payload["code"] == "approval_invalid"


def test_scope_hook_denies_write_but_keeps_read_frictionless(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    identity = {"user_id": 11, "id": 2, "scopes": ["read"]}
    read_context = governance.new_tool_context(
        identity, "list_records", {"file_id": "f", "sheet_id": 1},
    )
    write_context = governance.new_tool_context(
        identity,
        "update_records",
        {"file_id": "f", "sheet_id": 1, "records": [{"id": "r1", "状态": "完成"}]},
    )
    assert asyncio.run(governance.run_pre_tool_hooks(read_context)).action == "allow"
    denied = asyncio.run(governance.run_pre_tool_hooks(write_context))
    assert denied.action == "deny"
    assert denied.payload["required_scope"] == "write_records"


def test_redaction_hides_credentials_and_attachment_content():
    preview = json.loads(governance.redacted_arguments({
        "api_key": "secret-key",
        "file_base64": "very-large-content",
        "nested": {"password": "secret-password", "safe": "visible"},
    }))
    assert preview["api_key"] == "***"
    assert preview["file_base64"] == "***"
    assert preview["nested"]["password"] == "***"
    assert preview["nested"]["safe"] == "visible"


def test_mcp_tool_schema_exposes_approval_and_standard_annotations():
    from core.mcp_server import mcp_server

    destructive = mcp_server._tool_manager.get_tool("delete_records")
    read_only = mcp_server._tool_manager.get_tool("list_records")
    assert "approval_id" in destructive.parameters["properties"]
    assert destructive.annotations.destructiveHint is True
    assert destructive.annotations.readOnlyHint is False
    assert "approval_id" not in read_only.parameters["properties"]
    assert read_only.annotations.readOnlyHint is True


def test_mcp_execution_waits_for_approval_then_runs_once(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    from agent import assistant as assistant_module
    from core import mcp_server as service

    calls = []

    async def fake_wps_token(user_id):
        return "wps-token"

    def fake_delete(token, arguments):
        calls.append((token, dict(arguments)))
        return {"ok": True, "deleted": len(arguments["record_ids"])}

    monkeypatch.setattr(service, "_wps_access_token", fake_wps_token)
    monkeypatch.setitem(assistant_module.TOOL_MAP, "delete_records", fake_delete)
    identity = {"user_id": 12, "id": 88, "scopes": ["all"]}
    reset_token = service._identity_var.set(identity)
    arguments = {"file_id": "f", "sheet_id": 1, "record_ids": ["r1"]}
    try:
        first = asyncio.run(service.execute_tool("delete_records", arguments))
        assert first["code"] == "approval_required"
        assert calls == []
        db.decide_mcp_tool_approval(12, first["approval_id"], "approved")

        executed = asyncio.run(service.execute_tool(
            "delete_records", {**arguments, "approval_id": first["approval_id"]},
        ))
        assert executed == {"ok": True, "deleted": 1}
        assert calls == [("wps-token", arguments)]

        replay = asyncio.run(service.execute_tool(
            "delete_records", {**arguments, "approval_id": first["approval_id"]},
        ))
        assert replay["code"] == "approval_invalid"
        assert len(calls) == 1
    finally:
        service._identity_var.reset(reset_token)

    logs = db.list_mcp_audit_log(12)
    assert {row["decision"] for row in logs} >= {"ask", "executed"}
