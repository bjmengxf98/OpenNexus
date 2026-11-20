"""Knowledge excerpt/full-read contracts using synthetic, temporary SQLite only."""
import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock, Mock

import pytest

from auth import db
from agent import assistant, tool_planner
from core import knowledge_rag, mcp_server
from mcp.server.fastmcp.exceptions import ToolError


@pytest.fixture(autouse=True)
def knowledge_db(monkeypatch, tmp_path):
    path = tmp_path / "knowledge.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE org_knowledge (id INTEGER PRIMARY KEY, title TEXT, content TEXT, file_name TEXT, is_enabled INTEGER)")
    monkeypatch.setattr(assistant, "_db_get_embed_config", lambda: None)
    monkeypatch.setattr(knowledge_rag, "embed_texts", AsyncMock(side_effect=AssertionError("No real embedding calls")))
    monkeypatch.setattr(mcp_server, "_wps_access_token", AsyncMock(side_effect=AssertionError("Knowledge must not access WPS")))
    monkeypatch.setattr(mcp_server.db, "add_mcp_audit_log", Mock())
    return path


def seed(text, *, kid=1, enabled=True, title="格式规范"):
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("INSERT INTO org_knowledge VALUES (?, ?, ?, ?, ?)", (kid, title, text, "example.md", int(enabled)))


def call(**args):
    return asyncio.run(assistant.TOOL_MAP["search_knowledge"]("", args))


def test_keyword_search_exposes_late_hit_and_original_text_reference():
    text = "开头说明。" * 600 + "\n5.4 版记\n页码应按要求编排。\n" + "末尾说明。" * 600
    seed(text)
    result = call(query="版记")
    item = result["items"][0]
    assert "5.4 版记" in item["content"]
    assert len(item["content"]) <= 2000 and item["is_complete"] is False
    assert item["content"] == text[item["offset"]:item["end_offset"]]
    read = call(**item["read_arguments"])
    assert read["items"][0]["content"] == text[:6000]


def test_paging_reconstructs_every_character_including_late_sections():
    text = "📄规范甲乙\n" * 1600 + "5.4 版记\n最后条文。"
    seed(text)
    ref = call(query="格式规范")["items"][0]["read_arguments"]
    ref["limit"] = 777
    pages = []
    while True:
        result = call(**ref)
        page = result["items"][0]
        assert page["document_version"] == ref["version"]
        assert page["offset"] == sum(map(len, pages))
        pages.append(page["content"])
        assert result["is_complete"] is (len(pages) == 1 and not page["has_more"])
        if not page["has_more"]:
            break
        ref = {**page["read_arguments"], "offset": page["next_offset"], "limit": 777}
    assert "".join(pages) == text


def test_small_document_is_returned_whole_without_artificial_truncation():
    seed("短条文。")
    result = call(query="格式规范")
    assert result["items"][0]["is_complete"] is True
    page = call(knowledge_id=1)
    assert page["is_complete"] is True and page["has_more"] is False
    assert page["items"][0]["content"] == "短条文。"


def test_rag_uses_actual_late_chunk_not_the_document_prefix(monkeypatch):
    text = "旧开头。" * 800 + "\n专项条款：页码版记要求。\n" + "文末。" * 800
    seed(text)
    monkeypatch.setattr(assistant, "_db_get_embed_config", lambda: {"api_key": "fake", "base_url": "https://invalid.example", "model": "fake"})
    embed = AsyncMock(return_value=[[1.0]])
    monkeypatch.setattr(knowledge_rag, "embed_texts", embed)
    monkeypatch.setattr(assistant, "_db_search_knowledge_rag", lambda *a, **kw: [{
        "id": 1, "title": "格式规范", "content": text, "file_name": "example.md",
        "chunk_text": "专项条款：页码版记要求。", "score": 1.0,
    }])
    result = call(query="公文排版")
    item = result["items"][0]
    assert "专项条款：页码版记要求。" in item["content"] and item["offset"] > 2000
    assert item["content"] == text[item["offset"]:item["end_offset"]]
    assert call(**item["read_arguments"])["found"] is True
    embed.assert_awaited_once()  # Direct full reads do not embed or re-search.


def test_stale_rag_chunk_is_never_substituted_for_stored_original(monkeypatch):
    from core.knowledge_access import knowledge_excerpt
    row = {"id": 1, "title": "规范", "file_name": "a.md", "content": "当前原文" * 800,
           "chunk_text": "已经删掉的旧条款"}
    item = knowledge_excerpt(row, "规范")
    assert "已经删掉的旧条款" not in item["content"]
    assert item["content"] == row["content"][item["offset"]:item["end_offset"]]


def test_disabled_or_deleted_items_cannot_be_read_even_with_prior_reference():
    seed("不公开条文。", enabled=False)
    assert call(query="格式规范")["found"] is False
    assert call(knowledge_id=1)["found"] is False
    assert call(knowledge_id=999)["found"] is False


def test_disabling_after_first_page_revokes_further_reads():
    seed("原文条款。" * 2000)
    page = call(knowledge_id=1, limit=100)["items"][0]
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("UPDATE org_knowledge SET is_enabled=0 WHERE id=1")
    assert call(**{**page["read_arguments"], "offset": 100})["found"] is False


def test_embedding_failure_preserves_keyword_match_and_full_read(monkeypatch):
    seed("序言。" * 1000 + "末尾唯一规定")
    monkeypatch.setattr(assistant, "_db_get_embed_config", lambda: {"api_key": "fake", "base_url": "https://invalid.example", "model": "fake"})
    monkeypatch.setattr(knowledge_rag, "embed_texts", AsyncMock(side_effect=RuntimeError("simulated unavailable embedding")))
    found = call(query="末尾唯一规定")
    assert "末尾唯一规定" in found["items"][0]["content"]
    assert "末尾唯一规定" in call(**found["items"][0]["read_arguments"])["items"][0]["content"]


def test_read_offsets_without_document_id_are_not_silently_ignored():
    seed("条文" * 2000)
    assert call(query="格式规范", offset=2000).get("error")


def test_update_between_pages_requires_restart_instead_of_mixing_versions():
    seed("原文。" * 2000)
    page = call(knowledge_id=1, limit=100)["items"][0]
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("UPDATE org_knowledge SET content=? WHERE id=1", ("新内容。" * 2000,))
    result = call(**{**page["read_arguments"], "offset": page["next_offset"], "limit": 100})
    assert result["code"] == "knowledge_version_changed" and not result.get("items")
    assert call(knowledge_id=1)["items"][0]["content"].startswith("新内容。")


@pytest.mark.parametrize("args", [
    {"knowledge_id": True}, {"knowledge_id": -1}, {"knowledge_id": "../../data/app.db"},
    {"knowledge_id": 1, "offset": -1}, {"knowledge_id": 1, "offset": 1},
    {"knowledge_id": 1, "offset": True}, {"knowledge_id": 1, "limit": 0},
    {"knowledge_id": 1, "limit": 8001}, {"knowledge_id": 1, "limit": "1000"},
])
def test_invalid_read_arguments_never_return_document_text(args):
    seed("测试条文" * 1000)
    assert call(**args).get("error")


def test_folded_knowledge_pages_keep_bounded_restore_reference():
    seed("知识正文。" * 2500)
    result = call(knowledge_id=1)
    messages = [{"role": "tool", "content": json.dumps(result, ensure_ascii=False)}]
    compact, count = tool_planner.compact_tool_messages(messages, preserve_recent_tool_messages=0)
    folded = json.loads(compact[0]["content"])
    assert count == 1 and len(compact[0]["content"]) < 2500
    args = folded["_knowledge_read"][0]
    restored = call(**args)
    assert restored["items"][0]["content"] == result["items"][0]["content"]


@pytest.mark.parametrize("scopes,allowed", [(["read"], True), (["manage_memory"], False)])
def test_mcp_shares_local_read_without_wps_or_expanded_scopes(scopes, allowed):
    seed("末尾完整内容" * 1000)
    args = {"knowledge_id": 1}
    marker = mcp_server._identity_var.set({"user_id": 7, "id": 12, "scopes": scopes})
    try:
        if allowed:
            local = call(**args)
            remote = asyncio.run(mcp_server.execute_tool("search_knowledge", args))
            assert local == remote and remote["found"] is True
        else:
            with pytest.raises(ToolError):
                asyncio.run(mcp_server.execute_tool("search_knowledge", args))
        mcp_server._wps_access_token.assert_not_awaited()
    finally:
        mcp_server._identity_var.reset(marker)


def test_chat_model_boundary_can_search_then_read_original_without_wps(monkeypatch):
    from types import SimpleNamespace
    seed("前言。" * 900 + "后半段完整条文。")
    seen = []

    async def create(**kwargs):
        tools = {t["function"]["name"]: t["function"] for t in kwargs["tools"]}
        assert "knowledge_id" in tools["search_knowledge"]["parameters"]["properties"]
        tool_messages = [m for m in kwargs["messages"] if m["role"] == "tool"]
        if len(seen) == 0:
            arguments = {"query": "格式规范"}
        elif len(seen) == 1:
            arguments = json.loads(tool_messages[-1]["content"])["items"][0]["read_arguments"]
        else:
            assert len(seen) == 2
            assert "后半段完整条文。" in json.loads(tool_messages[-1]["content"])["items"][0]["content"]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="已读到后半段完整条文。", tool_calls=None, reasoning_content=None))])
        seen.append(arguments)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=None, reasoning_content=None, tool_calls=[SimpleNamespace(
                id=f"knowledge-{len(seen)}", function=SimpleNamespace(
                    name="search_knowledge", arguments=json.dumps(arguments)))]))])

    agent = assistant.Assistant.__new__(assistant.Assistant)
    agent.provider, agent.model = "mock", "mock-model"
    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    agent.supports_tools, agent.supports_vision = True, False
    agent.reasoning_mode, agent.reasoning_effort = "off", "auto"
    agent.context_window, agent.max_output_tokens = 0, 1024
    agent.smart_tool_routing = True
    agent._auto_learn = AsyncMock()
    monkeypatch.setattr(assistant, "_db_get_system_config", lambda *args, **kwargs: "")
    result = asyncio.run(agent.chat([{"role": "user", "content": "请完整阅读格式规范"}], access_token=""))
    assert result == "已读到后半段完整条文。"
    assert len(seen) == 2 and "knowledge_id" in seen[1]
