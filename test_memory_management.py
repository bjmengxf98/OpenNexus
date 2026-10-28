"""Memory edits use temporary databases and never execute stored commands."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from auth import db
from agent import assistant
from agent.tool_planner import discover_tool_names, needs_evidence_review
from core import mcp_server as service
from core.context_memory import build_user_context, recall_chat_history
from core.memory_management import execute_memory_tool, MEMORY_TOOL_NAMES
from core.tool_governance import policy_for_tool


OLD = "旧的重启命令：restart-old --port 8000"
NEW = "新的重启命令：restart-new --port 8000"


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "memory.db")
    db.init_db()
    with db.get_conn() as conn:
        for uid in (1, 2):
            conn.execute("INSERT INTO users (id,username,email,password_hash) VALUES (?,?,?,'x')",
                         (uid, f"user-{uid}", f"user-{uid}@example.test"))
    monkeypatch.setattr(service, "_wps_access_token", AsyncMock(side_effect=AssertionError("no WPS needed")))


def reference(content=OLD, uid=1):
    return next(row["memory_id"] for row in db.list_manageable_memories(uid)["memories"]
                if row["content"] == content)


def test_list_search_pagination_includes_legacy_without_duplicate_global_copy():
    db.save_memory_item(1, OLD)
    db.save_user_memory(1, f"- {OLD}\n- legacy-only command")
    db.save_memory_item(2, "other user's command")
    page = execute_memory_tool("list_memories", {"query": "command", "limit": 1}, 1)
    assert page["total"] == 1 and page["memories"][0]["content"] == "legacy-only command"
    all_rows = execute_memory_tool("list_memories", {"limit": 1}, 1)
    assert all_rows["total"] == 2 and all_rows["next_offset"] == 1
    next_rows = execute_memory_tool("list_memories", {"offset": 1}, 1)
    assert next_rows["memories"][0]["memory_id"].startswith("legacy:")


def test_delete_removes_exact_copies_and_legacy_only_for_owner():
    db.save_memory_item(1, OLD)
    db.save_memory_item(1, OLD, scope_type="file", scope_id="file-a")
    db.save_memory_item(1, "keep this preference")
    db.save_memory_item(2, OLD)
    db.save_user_memory(1, f"- {OLD}\n- keep this preference")
    db.save_user_memory(2, f"- {OLD}")
    memory_id = reference()
    result = execute_memory_tool("delete_memory", {"memory_id": memory_id}, 1)
    assert result["verified"] and result["deleted_count"] == 2 and result["legacy_removed"] == 1
    assert [row["content"] for row in db.list_memory_items(1)] == ["keep this preference"]
    assert OLD not in db.get_user_memory(1)
    assert OLD in db.get_user_memory(2)
    assert db.list_memory_items(2)[0]["content"] == OLD
    assert execute_memory_tool("delete_memory", {"memory_id": memory_id}, 1)["already_deleted"]


def test_legacy_multiline_entry_deletes_as_one_item_and_retry_is_idempotent():
    old = "old restart card\nrestart-old --port 8000\ncheck status"
    db.save_user_memory(1, "- " + old + "\n- retain other card")
    memory_id = reference(old)
    result = execute_memory_tool("delete_memory", {"memory_id": memory_id}, 1)
    assert result["legacy_removed"] == 1
    assert db.get_user_memory(1) == "- retain other card"
    assert execute_memory_tool("delete_memory", {"memory_id": memory_id}, 1)["already_deleted"]


def test_versioned_reference_rejects_changed_content_and_cross_user():
    item_id = db.save_memory_item(1, OLD)
    old_ref = reference()
    with pytest.raises(ValueError):
        execute_memory_tool("delete_memory", {"memory_id": old_ref}, 2)
    with db.get_conn() as conn:
        conn.execute("UPDATE memory_items SET content=? WHERE id=?", (NEW, item_id))
    with pytest.raises(ValueError):
        execute_memory_tool("delete_memory", {"memory_id": old_ref}, 1)
    assert db.list_memory_items(1)[0]["content"] == NEW


@pytest.mark.parametrize("content", ["", OLD, "current table file_id=private", "valid text\nfile_id=private"])
def test_invalid_replacement_preserves_old_memory_and_legacy(content):
    db.save_memory_item(1, OLD)
    db.save_user_memory(1, "- " + OLD)
    with pytest.raises(ValueError):
        execute_memory_tool("replace_memory", {"memory_id": reference(), "content": content}, 1)
    assert db.list_memory_items(1)[0]["content"] == OLD
    assert db.get_user_memory(1) == "- " + OLD
    assert not db.get_forgotten_memory_contents(1)


def test_replace_preserves_scope_and_old_deletion_is_atomic_on_failure():
    db.save_memory_item(1, OLD, scope_type="file", scope_id="file-a")
    db.save_user_memory(1, "- " + OLD)
    memory_id = reference()
    with db.get_conn() as conn:
        conn.execute("CREATE TRIGGER refuse_new_memory BEFORE INSERT ON memory_items "
                     "WHEN NEW.content='blocked-new' BEGIN SELECT RAISE(ABORT,'simulated write failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        execute_memory_tool("replace_memory", {"memory_id": memory_id, "content": "blocked-new"}, 1)
    assert db.list_memory_items(1)[0]["content"] == OLD
    assert db.get_user_memory(1) == "- " + OLD
    assert not db.get_forgotten_memory_contents(1)
    result = execute_memory_tool("replace_memory", {"memory_id": memory_id, "content": NEW}, 1)
    assert result["verified"] and result["memory_id"] != memory_id
    assert [(row["content"], row["scope_type"], row["scope_id"]) for row in db.list_memory_items(1)] == [
        (NEW, "file", "file-a")]
    assert OLD not in db.get_user_memory(1) and NEW not in db.get_user_memory(1)


def test_global_replacement_updates_compatibility_store():
    db.save_user_memory(1, "- " + OLD)
    result = execute_memory_tool("replace_memory", {"memory_id": reference(), "content": NEW}, 1)
    assert result["verified"]
    assert db.get_user_memory(1) == "- " + NEW
    assert db.list_memory_items(1)[0]["content"] == NEW


def test_legacy_readback_failure_rolls_back_the_entire_replacement():
    db.save_memory_item(1, OLD)
    db.save_user_memory(1, "- " + OLD)
    with db.get_conn() as conn:
        conn.execute("CREATE TRIGGER restore_old_legacy AFTER UPDATE ON user_memory "
                     "BEGIN UPDATE user_memory SET memory_text=OLD.memory_text WHERE user_id=NEW.user_id; END")
    with pytest.raises(RuntimeError, match="核验失败"):
        execute_memory_tool("replace_memory", {"memory_id": reference(), "content": NEW}, 1)
    assert db.list_memory_items(1)[0]["content"] == OLD
    assert db.get_user_memory(1) == "- " + OLD
    assert not db.get_forgotten_memory_contents(1)


def test_background_summary_history_and_stale_legacy_do_not_restore_deleted_content(monkeypatch):
    conv_id = db.create_conversation(1, "restart command")
    db.add_chat(1, "user", "remember this: " + OLD, conv_id=conv_id)
    db.save_memory_item(1, OLD)
    db.save_user_memory(1, "- " + OLD)
    # Existing derived copies must also stop supplying obsolete content.
    db.save_memory_item(1, "summary: " + OLD, scope_type="conversation", scope_id=str(conv_id),
                        source_type="conversation_summary", source_id=str(conv_id))
    execute_memory_tool("delete_memory", {"memory_id": reference()}, 1)
    assert db.save_memory_item(1, "summary: " + OLD, source_type="automatic") == 0
    assert db.save_memory_item(1, OLD, scope_type="conversation", scope_id=str(conv_id),
                               source_type="conversation_summary", source_id=str(conv_id), replace_source=True) == 0
    db.save_user_memory(1, "- " + OLD + "\n- keep new preference")
    assert OLD not in db.get_user_memory(1)
    assert not recall_chat_history(1, "重启命令 restart-old")
    assert OLD not in build_user_context(1, conv_id, "restart", legacy_memory="- " + OLD)
    assert not db.list_manageable_memories(1, query=OLD)["memories"]
    monkeypatch.setattr(db, "list_memory_items", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError()))
    assert OLD not in build_user_context(1, conv_id, "restart", legacy_memory="- " + OLD)
    assert db.get_chat_history(1, conv_id=conv_id)[0]["content"] == "remember this: " + OLD


def test_explicit_user_save_can_restore_forgotten_content():
    db.save_memory_item(1, OLD)
    execute_memory_tool("delete_memory", {"memory_id": reference()}, 1)
    db.save_memory_item(1, OLD, source_type="explicit")
    db.save_user_memory(1, "- " + OLD)
    assert db.list_memory_items(1)[0]["content"] == OLD
    assert db.get_user_memory(1) == "- " + OLD
    assert not db.get_forgotten_memory_contents(1)


def test_short_deleted_entry_does_not_suppress_unrelated_facts():
    db.save_memory_item(1, "A")
    execute_memory_tool("delete_memory", {"memory_id": reference("A")}, 1)
    assert db.save_memory_item(1, "Airport project runs on Friday", source_type="automatic")
    assert db.list_memory_items(1)[0]["content"] == "Airport project runs on Friday"


@pytest.mark.parametrize("correction", [False, True])
def test_real_auto_learning_cannot_bypass_deleted_guard_through_legacy_write(correction):
    db.save_memory_item(1, OLD)
    execute_memory_tool("delete_memory", {"memory_id": reference()}, 1)
    extracted = ("[纠错] " if correction else "[自动] ") + OLD
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=extracted))])
    agent = assistant.Assistant.__new__(assistant.Assistant)
    create = AsyncMock(return_value=response)
    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    user_msg = "纠正，正确的是旧的部署安排" if correction else "我们正在回顾以前的部署安排"
    asyncio.run(agent._auto_learn(user_msg, "", 1, "mock"))
    assert not db.get_user_memory(1)
    assert not db.list_memory_items(1)
    assert create.await_count == 1


@pytest.mark.parametrize("name", ["list_memories", "delete_memory", "replace_memory"])
def test_memory_mcp_scopes_and_owner_identity(monkeypatch, name):
    db.save_memory_item(1, OLD)
    args = {} if name == "list_memories" else {"memory_id": reference()}
    if name == "replace_memory":
        args["content"] = NEW
    identity = {"user_id": 1, "token_id": 9, "scopes": ["read"], "username": "u1"}
    monkeypatch.setattr(service, "_identity", lambda: identity)
    monkeypatch.setattr("core.tool_governance.db.add_mcp_audit_log", lambda *_args, **_kwargs: None)
    with pytest.raises(ToolError):
        asyncio.run(service.execute_tool(name, args))
    identity["scopes"] = ["manage_memory"]
    if name != "list_memories":
        with pytest.raises(ToolError):
            asyncio.run(service.execute_tool(name, args))
        identity["scopes"].append("delete_data")
    result = asyncio.run(service.execute_tool(name, args))
    assert result["ok"]
    service._wps_access_token.assert_not_awaited()
    assert policy_for_tool(name).read_only is (name == "list_memories")


def test_real_chat_dispatches_memory_delete_without_wps_or_extra_model(monkeypatch):
    db.save_memory_item(1, OLD)
    memory_id = reference()
    responses = [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=None, reasoning_content=None, tool_calls=[SimpleNamespace(id="forget-1", function=SimpleNamespace(
            name="delete_memory", arguments=json.dumps({"memory_id": memory_id})))]))]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="已删除旧记忆。", reasoning_content=None, tool_calls=None))])]
    agent = assistant.Assistant.__new__(assistant.Assistant)
    agent.provider, agent.model = "mock", "mock"
    create = AsyncMock(side_effect=responses)
    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    agent.supports_tools = True
    agent.smart_tool_routing = True
    agent._auto_learn = AsyncMock()
    asyncio.run(agent.chat([{"role": "user", "content": "删除刚才明确选中的旧记忆"}], "", uid=1))
    assert agent.last_tool_receipts[0]["result"]["verified"] is True
    assert not db.list_memory_items(1)
    assert create.await_count == 2


def test_catalog_exposes_memory_tools_and_rejects_caller_user_override():
    catalog = asyncio.run(service.mcp_server.list_tools())
    assert MEMORY_TOOL_NAMES <= {item.name for item in catalog}
    with pytest.raises(ValueError):
        execute_memory_tool("list_memories", {"user_id": 2}, 1)


def test_memory_mutation_discovery_includes_query_dependency_and_needs_no_read_only_review():
    for name in ("delete_memory", "replace_memory"):
        names, _ = discover_tool_names(name, assistant.TOOLS, limit=1)
        assert {name, "list_memories"} <= set(names)
        assert not needs_evidence_review([
            {"name": "list_memories", "result": {"ok": True}},
            {"name": "list_memories", "result": {"ok": True}},
            {"name": name, "result": {"ok": True, "verified": True}},
        ])
