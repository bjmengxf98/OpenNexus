import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock, Mock

import pytest

from auth import db
from core import tool_governance as governance


def _fresh_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "governance.db")
    db.init_db()


def test_scope_validation_and_policy_classification():
    assert governance.normalize_requested_scopes(None) == ["read"]
    assert governance.normalize_requested_scopes(["read", "write_records", "read"]) == [
        "read", "write_records",
    ]
    assert governance.granted_scopes(None) == set()

    assert governance.policy_for_tool("list_records").read_only is True
    assert governance.policy_for_tool("delete_records").destructive is True
    assert governance.policy_for_tool("send_weixin_message").risk == "external_message"
    assert governance.policy_for_tool(
        "update_records", {"records": [{"id": "1"}, {"id": "2"}]},
    ).approval_required is False
    assert governance.policy_for_tool(
        "update_records", {"records": [{"id": "1"}, {"id": "2"}]},
    ).risk == "batch_write"
    assert governance.policy_for_tool(
        "update_records", {"records": [{"id": "1"}]},
    ).approval_required is False


def test_new_tokens_default_to_read_and_existing_grants_are_not_expanded(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    legacy = db.create_mcp_token(7, "旧全部权限客户端", scopes=["all"])
    default = db.create_mcp_token(7, "新默认客户端")
    limited = db.create_mcp_token(7, "限权客户端", scopes=["write_records"])

    assert legacy["scopes"] == ["all"]
    assert default["scopes"] == ["read"]
    rows = {row["id"]: row for row in db.list_mcp_tokens(7)}
    assert rows[legacy["id"]]["scopes"] == ["all"]
    assert rows[limited["id"]]["scopes"] == ["write_records"]
    assert "delete_data" not in rows[limited["id"]]["scopes"]
    assert all("token" not in row for row in rows.values())
    with pytest.raises(ValueError, match="至少选择"):
        db.create_mcp_token(7, scopes=[])


@pytest.mark.parametrize("value", [[], ["unknown"], ["all", "unknown"], {}, 1])
def test_invalid_new_token_permissions_are_rejected(value):
    with pytest.raises(ValueError):
        governance.normalize_requested_scopes(value)


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


@pytest.mark.parametrize("tool,scopes,args,allowed", [
    ("send_weixin_message", ["send_messages"], {"text": "测试"}, True),
    ("send_wecom_message", ["send_messages"], {}, True),
    ("send_notification", ["read"], {}, False),
    ("update_records", ["write_records"], {"records": [{}, {}]}, True),
    ("create_records", ["write_records"], {"records": [{}, {}]}, True),
    ("sheets_update_range", ["write_records"], {"range_data": [{}, {}]}, True),
    ("delete_records", ["write_records"], {}, False),
    ("delete_records", ["delete_data"], {}, True),
    ("sheets_delete_range", ["delete_data"], {}, True),
    ("delete_sheet", ["manage_structure"], {}, False),
    ("delete_fields", ["delete_data"], {}, False),
    ("delete_fields", ["manage_structure", "delete_data"], {}, True),
    ("delete_view", ["manage_structure", "delete_data"], {}, True),
    ("delete_hook", ["manage_structure", "delete_data"], {}, True),
    ("sheets_delete_worksheets", ["manage_structure", "delete_data"], {}, True),
    ("unbind_children", ["write_records"], {}, False),
    ("unbind_children", ["write_records", "delete_data"], {}, True),
    ("disable_parent", ["manage_structure", "delete_data"], {}, True),
    ("create_fields", ["manage_structure"], {}, True),
    ("analyze_records", ["read"], {}, True),
    ("delete_records", ["all"], {}, True),
])
def test_preauthorization_allows_or_denies_without_creating_approvals(monkeypatch, tool, scopes, args, allowed):
    create = Mock(side_effect=AssertionError("must not create approval"))
    consume = Mock(side_effect=AssertionError("must not consume approval"))
    monkeypatch.setattr(db, "create_mcp_tool_approval", create)
    monkeypatch.setattr(db, "consume_mcp_tool_approval", consume)
    context = governance.new_tool_context({"user_id": 9, "id": 1, "scopes": scopes}, tool, args)
    decision = asyncio.run(governance.run_pre_tool_hooks(context))
    assert decision.action == ("allow" if allowed else "deny")
    assert context.policy.approval_required is False
    assert governance.potentially_requires_approval(tool) is False
    create.assert_not_called()
    consume.assert_not_called()


def test_unclassified_tools_are_denied_even_for_all_permission_tokens():
    context = governance.new_tool_context({"user_id": 9, "id": 1, "scopes": ["all"]}, "future_tool", {})
    decision = asyncio.run(governance.run_pre_tool_hooks(context))
    assert decision.action == "deny"
    assert decision.payload["code"] == "tool_unclassified"


@pytest.mark.parametrize("scopes,expected", [(["all"], "approval_retired"), (["read"], "scope_denied")])
def test_old_approval_ids_cannot_replay_or_grant_new_permissions(scopes, expected):
    context = governance.new_tool_context(
        {"user_id": 9, "id": 1, "scopes": scopes}, "delete_records",
        {"file_id": "f", "sheet_id": 1, "record_ids": ["r1"], "approval_id": "old-or-used-id"},
    )
    decision = asyncio.run(governance.run_pre_tool_hooks(context))
    assert decision.action == "deny"
    assert decision.payload["code"] == expected


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


def test_mcp_tool_schema_exposes_preauthorization_and_true_risk_annotations():
    from core.mcp_server import mcp_server

    destructive = mcp_server._tool_manager.get_tool("delete_records")
    read_only = mcp_server._tool_manager.get_tool("list_records")
    assert "approval_id" not in destructive.parameters["properties"]
    assert destructive.annotations.destructiveHint is True
    assert destructive.annotations.readOnlyHint is False
    assert "approval_id" not in read_only.parameters["properties"]
    assert read_only.annotations.readOnlyHint is True
    for name, tool in mcp_server._tool_manager._tools.items():
        assert name in governance._KNOWN_TOOLS
        assert "审批单" not in tool.description
        assert "不要求逐次审批" in tool.description
        assert "approval_id" not in tool.parameters["properties"]
        assert tool.parameters["additionalProperties"] is False


def test_mcp_delete_executes_once_without_approval_and_is_audited(monkeypatch, tmp_path):
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
    identity = {"user_id": 12, "id": 88, "scopes": ["delete_data"]}
    reset_token = service._identity_var.set(identity)
    arguments = {"file_id": "f", "sheet_id": 1, "record_ids": ["r1"]}
    try:
        executed = asyncio.run(service.execute_tool("delete_records", arguments))
        assert executed == {"ok": True, "deleted": 1}
        assert calls == [("wps-token", arguments)]

        assert db.list_mcp_tool_approvals(12) == []
    finally:
        service._identity_var.reset(reset_token)

    logs = db.list_mcp_audit_log(12)
    assert len(logs) == 1
    assert logs[0]["decision"] == "executed"
    assert logs[0]["risk"] == "destructive"
    assert logs[0]["required_scope"] == "delete_data"


def test_mcp_message_permission_is_token_scoped_and_uses_its_user(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    from core import mcp_server as service
    from mcp.server.fastmcp.exceptions import ToolError
    sender = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(service, "_send_weixin", sender)
    args = {"to_username": "测试收件人", "text": "测试内容"}
    allowed_identity = {"user_id": 12, "id": 88, "scopes": ["send_messages"]}
    marker = service._identity_var.set(allowed_identity)
    try:
        assert asyncio.run(service.execute_tool("send_weixin_message", args)) == {"ok": True}
    finally:
        service._identity_var.reset(marker)
    sender.assert_awaited_once_with(allowed_identity, args)

    marker = service._identity_var.set({"user_id": 13, "id": 89, "scopes": ["read"]})
    try:
        with pytest.raises(ToolError, match="发送外部消息"):
            asyncio.run(service.execute_tool("send_weixin_message", args))
    finally:
        service._identity_var.reset(marker)
    assert sender.await_count == 1
    assert db.list_mcp_tool_approvals(12) == []
    assert db.list_mcp_audit_log(12)[0]["decision"] == "executed"
    assert db.list_mcp_audit_log(13)[0]["decision"] == "deny"


def test_missing_delete_permission_prevents_any_wps_request(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    from agent import assistant as assistant_module
    from core import mcp_server as service
    from mcp.server.fastmcp.exceptions import ToolError
    wps_token, delete = AsyncMock(), AsyncMock()
    monkeypatch.setattr(service, "_wps_access_token", wps_token)
    monkeypatch.setitem(assistant_module.TOOL_MAP, "delete_records", delete)
    marker = service._identity_var.set({"user_id": 12, "id": 88, "scopes": ["write_records"]})
    try:
        with pytest.raises(ToolError, match="删除"):
            asyncio.run(service.execute_tool("delete_records", {"file_id": "f", "sheet_id": 1, "record_ids": ["r1"]}))
    finally:
        service._identity_var.reset(marker)
    wps_token.assert_not_awaited()
    delete.assert_not_awaited()
    assert db.list_mcp_audit_log(12)[0]["decision"] == "deny"


def test_expiry_revocation_and_disabled_user_still_block_tokens(monkeypatch, tmp_path):
    from datetime import datetime, timedelta
    _fresh_db(monkeypatch, tmp_path)
    assert db.create_user("token-owner", "owner@example.test", "test-password")[0]
    uid = db.get_user_by_username("token-owner")["id"]
    fixed_now = datetime(2026, 10, 25, 12, 0)
    monkeypatch.setattr(db, "beijing_now", lambda: fixed_now)
    token = db.create_mcp_token(uid, scopes=["send_messages"], expires_days=1)
    identity = db.verify_mcp_token(token["token"])
    assert identity["user_id"] == uid
    assert identity["scopes"] == ["send_messages"]
    monkeypatch.setattr(db, "beijing_now", lambda: fixed_now + timedelta(days=1))
    assert db.verify_mcp_token(token["token"]) is None
    monkeypatch.setattr(db, "beijing_now", lambda: fixed_now)
    db.set_user_enabled(uid, False)
    assert db.verify_mcp_token(token["token"]) is None
    db.set_user_enabled(uid, True)
    assert db.revoke_mcp_token(uid + 100, token["id"]) is False
    assert db.revoke_mcp_token(uid, token["id"]) is True
    assert db.verify_mcp_token(token["token"]) is None


def test_sdk_rejects_cached_approval_argument_before_any_side_effect(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    from core import mcp_server as service
    from mcp.server.fastmcp.exceptions import ToolError
    sender = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(service, "_send_weixin", sender)
    tool = service.mcp_server._tool_manager.get_tool("send_weixin_message")
    args = {"to_username": "测试收件人", "text": "测试内容"}
    marker = service._identity_var.set({"user_id": 12, "id": 88, "scopes": ["send_messages"]})
    try:
        with pytest.raises(ToolError, match="Extra inputs are not permitted"):
            asyncio.run(tool.run({**args, "approval_id": "old-used-id"}))
        sender.assert_not_awaited()
        assert asyncio.run(tool.run(args)) == {"ok": True}
        sender.assert_awaited_once()
    finally:
        service._identity_var.reset(marker)
