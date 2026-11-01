"""Memory provenance/version contracts use a fresh database, never real WPS."""
import asyncio
from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from auth import db
from core.context_memory import build_user_context
from core.memory_management import execute_memory_tool
from core import mcp_server as service
from agent import assistant


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "provenance.db")
    db.init_db()
    with db.get_conn() as conn:
        for uid in (1, 2):
            conn.execute("INSERT INTO users (id,username,email,password_hash) VALUES (?,?,?,'x')",
                         (uid, f"p{uid}", f"p{uid}@example.test"))


def memory_ref(content):
    return next(item["memory_id"] for item in db.list_manageable_memories(1)["memories"]
                if item["content"] == content)


def test_background_duplicate_preserves_explicit_origin_confidence_and_source():
    conv = db.create_conversation(1, "original instruction")
    original = db.save_memory_item(1, "keep answers concise", source_id=f"conversation:{conv}")
    assert db.save_memory_item(1, "keep answers concise", source_type="user_statement", confidence=.75) == original
    row = db.list_memory_items(1)[0]
    assert (row["source_type"], row["source_id"], row["confidence"]) == ("explicit", f"conversation:{conv}", 1)


def test_summary_revision_preserves_old_version_without_injecting_it_into_context():
    conv = db.create_conversation(1, "summary revisions")
    kwargs = dict(scope_type="conversation", scope_id=str(conv), source_type="conversation_summary",
                  source_id=str(conv), replace_source=True)
    first = db.save_memory_item(1, "old summary", **kwargs)
    second = db.save_memory_item(1, "new summary", **kwargs)
    assert second != first
    assert db.save_memory_item(1, "new summary", **kwargs) == second
    assert [r["content"] for r in db.list_memory_items(1)] == ["new summary"]
    rows = execute_memory_tool("list_memories", {"include_inactive": True}, 1)["memories"]
    old = next(r for r in rows if r["id"] == first)
    new = next(r for r in rows if r["id"] == second)
    assert old["status"] == "superseded" and new["status"] == "active"
    assert new["memory_id"] in old["replaced_by"]
    context = build_user_context(1, conv, "continue")
    assert "old summary" not in context and "new summary" in context


def test_explicit_replacement_exposes_version_relation_and_keeps_forget_guard():
    db.save_memory_item(1, "previous rule")
    old_ref = memory_ref("previous rule")
    changed = execute_memory_tool("replace_memory", {"memory_id": old_ref, "content": "updated rule"}, 1)
    rows = execute_memory_tool("list_memories", {"include_inactive": True}, 1)["memories"]
    old = next(r for r in rows if r["memory_id"] == old_ref)
    new = next(r for r in rows if r["memory_id"] == changed["memory_id"])
    assert old["status"] == "deleted" and new["replaces"] == old_ref
    assert new["memory_id"] in old["replaced_by"]
    assert db.save_memory_item(1, "previous rule", source_type="user_statement") == 0
    assert "previous rule" not in build_user_context(1, 0, "rule")


def test_memory_origin_is_scoped_and_missing_conversation_is_reported_honestly():
    conv = db.create_conversation(1, "source")
    db.save_memory_item(1, "explicit preference", source_id=f"conversation:{conv}")
    row = execute_memory_tool("list_memories", {}, 1)["memories"][0]
    assert row["source_id"] == f"conversation:{conv}" and row["source_available"] is True
    db.delete_conversation(conv, 1)
    row = execute_memory_tool("list_memories", {}, 1)["memories"][0]
    assert row["source_available"] is False  # Explicit long-term save survives source deletion.
    assert execute_memory_tool("list_memories", {"include_inactive": True}, 2)["memories"] == []


@pytest.mark.parametrize("source_type", ["user_statement", "conversation_summary"])
def test_deleted_source_cannot_receive_late_background_learning(source_type):
    conv = db.create_conversation(1, "deleted origin")
    db.delete_conversation(conv, 1)
    def source_args(value):
        return {"source_type": source_type,
                "source_id": str(value) if source_type == "conversation_summary" else f"conversation:{value}"}
    assert db.save_memory_item(1, "late generated preference", **source_args(conv)) == 0
    other = db.create_conversation(2, "other owner")
    assert db.save_memory_item(1, "cross owner origin", **source_args(other)) == 0
    assert not db.list_memory_items(1)


def test_context_distinguishes_confirmed_extracted_and_legacy_origins():
    db.save_memory_item(1, "confirmed preference")
    db.save_memory_item(1, "extracted preference", source_type="user_statement", confidence=.75)
    db.save_user_memory(1, "- confirmed preference\n- legacy preference")
    context = build_user_context(1, 0, "preference", legacy_memory=db.get_user_memory(1))
    assert "用户明确保存" in context and "自动提取" in context and "来源未记录" in context
    assert context.count("confirmed preference") == 1
    assert len(context) <= 7000


def test_legacy_origin_and_inactive_query_validation():
    db.save_user_memory(1, "- old compatibility entry")
    row = execute_memory_tool("list_memories", {}, 1)["memories"][0]
    assert row["source_type"] == "legacy" and row["source_available"] is None
    assert row["status"] == "active" and row["source_id"] == ""
    with pytest.raises(ValueError):
        execute_memory_tool("list_memories", {"include_inactive": "false"}, 1)


def test_operation_guides_are_lazy_versioned_and_do_not_accept_paths():
    from core.operation_guides import get_operation_guide
    index = get_operation_guide({})
    assert index["guides"] and all("content" not in r for r in index["guides"])
    for entry in index["guides"]:
        result = get_operation_guide({"guide_id": entry["guide_id"], "version": entry["version"]})
        assert result["content"] and result["applies_to"] and result["verification"]
        assert len(result["content"]) <= 6000
    assert get_operation_guide({"guide_id": "../../data/app.db"}).get("error")
    assert get_operation_guide({"guide_id": index["guides"][0]["guide_id"], "version": "outdated"}).get("error")


def test_disabled_guide_does_not_return_old_instructions(monkeypatch):
    from core import operation_guides as guides
    catalog = deepcopy(guides.GUIDES)
    first = next(iter(catalog))
    catalog[first]["status"] = "disabled"
    monkeypatch.setattr(guides, "GUIDES", catalog)
    assert all(r["guide_id"] != first for r in guides.get_operation_guide({})["guides"])
    result = guides.get_operation_guide({"guide_id": first})
    assert result.get("error") and "content" not in result


def test_guides_mcp_read_scope_needs_no_wps_and_does_not_expand_permissions(monkeypatch):
    async def no_wps(*args, **kwargs):
        raise AssertionError("Guide read must not renew WPS")
    monkeypatch.setattr(service, "_wps_access_token", no_wps)
    identity = {"user_id": 1, "token_id": 3, "scopes": ["manage_memory"], "username": "p1"}
    monkeypatch.setattr(service, "_identity", lambda: identity)
    with pytest.raises(ToolError):
        asyncio.run(service.execute_tool("get_operation_guide", {}))
    identity["scopes"] = ["read"]
    assert asyncio.run(service.execute_tool("get_operation_guide", {}))["guides"]
    assert "get_operation_guide" in {t.name for t in asyncio.run(service.mcp_server.list_tools())}


def test_summary_retention_is_bounded_without_pruning_deleted_guards():
    conv = db.create_conversation(1, "versions")
    db.save_memory_item(1, "forgotten preference")
    execute_memory_tool("delete_memory", {"memory_id": memory_ref("forgotten preference")}, 1)
    for index in range(7):
        db.save_memory_item(1, f"summary-{index}", scope_type="conversation", scope_id=str(conv),
                            source_type="conversation_summary", source_id=str(conv), replace_source=True)
    rows = execute_memory_tool("list_memories", {"include_inactive": True}, 1)["memories"]
    summaries = [r for r in rows if r["source_type"] == "conversation_summary"]
    assert len(summaries) == 3
    assert {r["content"] for r in summaries} == {"summary-4", "summary-5", "summary-6"}
    assert db.get_forgotten_memory_contents(1) == ["forgotten preference"]


def test_summary_insert_failure_keeps_original_active_version():
    conv = db.create_conversation(1, "failure")
    kwargs = dict(scope_type="conversation", scope_id=str(conv), source_type="conversation_summary",
                  source_id=str(conv), replace_source=True)
    first = db.save_memory_item(1, "valid prior summary", **kwargs)
    with db.get_conn() as conn:
        conn.execute("CREATE TRIGGER block_summary BEFORE INSERT ON memory_items "
                     "WHEN NEW.content='blocked summary' BEGIN SELECT RAISE(ABORT,'test failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        db.save_memory_item(1, "blocked summary", **kwargs)
    assert db.list_memory_items(1)[0]["id"] == first
    assert db.list_memory_items(1)[0]["status"] == "active"


def test_background_learning_links_actual_owner_conversation_and_late_result_is_rejected():
    conv = db.create_conversation(1, "user statement")
    agent = assistant.Assistant.__new__(assistant.Assistant)
    async def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="[自动] prefer short text"))])
    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    asyncio.run(agent._auto_learn("I prefer concise paragraphs when discussing routine updates", "", 1, "mock", conv_id=conv))
    assert db.list_memory_items(1)[0]["source_id"] == f"conversation:{conv}"
    db.delete_conversation(conv, 1)
    async def deleted_create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="[自动] late preference"))])
    agent.client.chat.completions.create = deleted_create
    asyncio.run(agent._auto_learn("I prefer concise paragraphs when discussing routine updates", "", 1, "mock", conv_id=conv))
    assert "late preference" not in db.get_user_memory(1)
    assert all(r["content"] != "[自动] late preference" for r in db.list_memory_items(1))


def test_chat_guide_lookup_and_explicit_save_preserve_origin_without_wps(monkeypatch):
    conv = db.create_conversation(1, "confirmed save")
    responses = [
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, reasoning_content=None,
            tool_calls=[SimpleNamespace(id="guide", function=SimpleNamespace(name="get_operation_guide",
                         arguments=json.dumps({"guide_id": "memory-versions"})))]))]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, reasoning_content=None,
            tool_calls=[SimpleNamespace(id="save", function=SimpleNamespace(name="save_memory",
                         arguments=json.dumps({"content": "confirmed concise style", "scope": "current_topic"})))]))]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="记忆已保存。", reasoning_content=None,
                                                                        tool_calls=None))]),
    ]
    agent = assistant.Assistant.__new__(assistant.Assistant)
    agent.provider, agent.model = "mock", "mock"
    agent.supports_tools, agent.smart_tool_routing = True, True
    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=responses))))
    agent._auto_learn = AsyncMock()
    async def no_wps(*args, **kwargs):
        raise AssertionError("No WPS read or renewal is needed")
    from agent import wps_client
    from auth import wps_session
    monkeypatch.setattr(wps_client, "_get", no_wps)
    monkeypatch.setattr(wps_client, "_post", no_wps)
    monkeypatch.setattr(wps_session, "ensure_access_token", no_wps)
    monkeypatch.setattr(assistant, "ensure_access_token", no_wps)
    async def run():
        reply = await agent.chat([{"role": "user", "content": "请核对记忆操作指南，只在当前话题记住我喜欢简洁表达"}],
                                 "", uid=1, conv_id=conv)
        await asyncio.sleep(0)
        return reply
    reply = asyncio.run(run())
    assert "保存" in reply
    assert [r["name"] for r in agent.last_tool_receipts] == ["get_operation_guide", "save_memory"]
    row = execute_memory_tool("list_memories", {}, 1)["memories"][0]
    assert row["source_id"] == f"conversation:{conv}" and row["source_available"] is True
    assert row["scope_type"] == "conversation" and row["scope_id"] == str(conv)
    assert not db.get_user_memory(1)
    agent._auto_learn.assert_not_awaited()


def test_operation_guide_result_is_detached_and_unknown_arguments_are_rejected():
    from core.operation_guides import get_operation_guide
    result = get_operation_guide({"guide_id": "memory-versions"})
    result["applies_to"].clear()
    assert get_operation_guide({"guide_id": "memory-versions"})["applies_to"]
    assert get_operation_guide({"file_path": "data/app.db"}).get("error")


@pytest.mark.parametrize("operation", ["insert", "replace", "update"])
def test_source_readback_mismatch_rolls_back_insert_or_replacement(operation):
    db.save_memory_item(1, "original preference")
    old_ref = memory_ref("original preference")
    if operation == "update":
        db.save_memory_item(1, "new preference")
    before = db.list_memory_items(1)
    with db.get_conn() as conn:
        event = "UPDATE" if operation == "update" else "INSERT"
        conn.execute(f"CREATE TRIGGER tamper_origin AFTER {event} ON memory_items "
                     "WHEN NEW.content='new preference' AND NEW.source_id!='tampered' "
                     "BEGIN UPDATE memory_items SET source_id='tampered' WHERE id=NEW.id; END")
    with pytest.raises(RuntimeError, match="核验失败"):
        if operation == "replace":
            execute_memory_tool("replace_memory", {"memory_id": old_ref, "content": "new preference"}, 1)
        else:
            db.save_memory_item(1, "new preference")
    assert db.list_memory_items(1) == before
    assert not db.get_forgotten_memory_contents(1)


@pytest.mark.parametrize("scope", ["current_topic", "invalid_scope"])
def test_chat_missing_or_invalid_scope_never_falls_back_to_global(scope):
    from test_current_goal_repair import make_assistant, response
    create = AsyncMock(side_effect=[response(name="save_memory", args={"content": "topic-only note", "scope": scope}),
                                    response("当前无法按指定范围保存。")])
    agent = make_assistant(create)
    asyncio.run(agent.chat([{"role": "user", "content": "只在当前话题保存这条测试记忆"}], "", uid=1))
    assert agent.last_tool_receipts[0]["result"].get("error")
    assert not db.list_memory_items(1) and not db.get_user_memory(1)


def test_mcp_invalid_memory_scope_is_rejected_without_saving(monkeypatch):
    identity = {"user_id": 1, "token_id": 4, "scopes": ["manage_memory"], "username": "p1"}
    monkeypatch.setattr(service, "_identity", lambda: identity)
    with pytest.raises(ToolError):
        asyncio.run(service.execute_tool("save_memory", {"content": "narrowly scoped note", "scope": "invalid_scope"}))
    assert not db.list_memory_items(1) and not db.get_user_memory(1)
