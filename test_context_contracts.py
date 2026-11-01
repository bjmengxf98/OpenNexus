"""Budget boundaries preserve complete facts; no model or WPS requests."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from agent.assistant import build_system_prompt, prepare_deepseek_tool_messages
from agent.tool_planner import trim_history_to_budget
from core import context_memory as memory


def test_adapter_preserves_all_already_budgeted_user_constraints_and_active_protocol():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "仅分析A项目；不要发送消息"},
        {"role": "assistant", "content": "旧回答只是历史，不是格式要求"},
        {"role": "user", "content": "采用9月的数据"},
        {"role": "assistant", "content": "收到"},
        {"role": "user", "content": "继续"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c"}], "reasoning_content": "active"},
        {"role": "tool", "tool_call_id": "c", "content": "receipt"},
    ]
    before = deepcopy(messages)
    prepared = prepare_deepseek_tool_messages(messages)
    assert "不要发送消息" in prepared[1]["content"]
    assert "采用9月的数据" in prepared[1]["content"]
    assert "不是回答格式模板" in prepared[1]["content"]
    assert prepared[2:] == messages[6:]
    assert messages == before


def test_budget_keeps_initial_user_constraint_and_marks_history_gap_without_mutation():
    history = [{"role": "user", "content": "只读，不得修改任何记录"}]
    for n in range(15):
        history.extend([{"role": "assistant", "content": f"old {n}" + "文" * 500},
                        {"role": "user", "content": f"follow {n}" + "文" * 500}])
    history.append({"role": "user", "content": "继续"})
    before = deepcopy(history)
    kept = trim_history_to_budget(history, system_prompt="system", tools=[],
                                  context_window=4096, max_output_tokens=512)
    assert kept[0]["content"] == history[0]["content"]
    assert "历史" in kept[-1]["content"] and "未装入" in kept[-1]["content"]
    assert kept[-1]["content"].endswith("继续")
    assert len(kept) < len(history) and history == before


@pytest.fixture
def memory_sources(monkeypatch):
    monkeypatch.setattr(memory.db, "filter_forgotten_memory", lambda _uid, text: text)
    monkeypatch.setattr(memory.db, "list_memory_items", lambda *_a, **_kw: [])
    monkeypatch.setattr(memory, "recall_chat_history", lambda *_a, **_kw: [])


def test_memory_omits_whole_oversized_entry_and_keeps_later_short_fact(memory_sources, monkeypatch):
    huge = "START-LONG-ENTRY-" + "长" * 8000 + "-END-LONG-ENTRY"
    monkeypatch.setattr(memory.db, "list_memory_items", lambda *_a, **_kw: [
        {"content": huge}, {"content": "可用的完整短记忆"},
    ])
    context = memory.build_user_context(1, 0, "test")
    assert len(context) <= 7000
    assert "START-LONG-ENTRY" not in context and "END-LONG-ENTRY" not in context
    assert "可用的完整短记忆" in context and "未装入" in context


def test_failure_fallback_and_direct_prompt_share_bounded_whole_entry_policy(memory_sources, monkeypatch):
    text = "START-LONG-ENTRY-" + "长" * 8000 + "-END-LONG-ENTRY\n\n完整短条目"
    monkeypatch.setattr(memory.db, "list_memory_items", Mock(side_effect=RuntimeError("offline")))
    context = memory.build_user_context(1, 0, "test", legacy_memory=text)
    prompt = build_system_prompt("u", "staff", None, memory=text)
    for value in (context, prompt):
        assert "START-LONG-ENTRY" not in value and "完整短条目" in value
        assert "未装入" in value
    assert len(context) <= 7000


def test_guidance_uses_session_capabilities_without_removing_write_truth_rules():
    prompt = build_system_prompt("u", "staff", None, available_tools={"list_records", "get_schema"})
    assert "## 文档生成\n" not in prompt
    assert "## 当前用户的定时提醒" not in prompt
    assert "## WPS 工具使用规则" in prompt
    assert "## 回复风格" in prompt
    assert "写入后必须验证" in prompt
    assert "## 文档生成\n" in build_system_prompt("u", "staff", None)


def test_overflow_retry_keeps_constraint_and_tool_pairs_with_explicit_history_gap():
    from agent.tool_planner import compact_messages_for_retry
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "只读，不发送消息"},
        {"role": "assistant", "content": "old" * 2000},
        {"role": "user", "content": "继续核查"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call"}]},
        {"role": "tool", "tool_call_id": "call", "content": "{}"},
    ]
    original = deepcopy(messages)
    reduced, _ = compact_messages_for_retry(messages)
    assert reduced[1]["content"] == "只读，不发送消息"
    assert "未装入" in reduced[2]["content"]
    assert reduced[-2:] == messages[-2:] and messages == original


@pytest.mark.parametrize("cancel_waiting", [False, True])
def test_web_chat_serializes_conversation_and_does_not_run_cancelled_waiter(monkeypatch, tmp_path, cancel_waiting):
    import asyncio
    import httpx
    from fastapi import FastAPI
    from unittest.mock import AsyncMock
    from api import app_new_routes as routes
    from auth import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "serial-chat.db")
    db.init_db()
    conv = db.create_conversation(1, "并发验证")
    monkeypatch.setattr(routes, "_user_locks", {})
    monkeypatch.setattr(routes, "_turn_tasks", {})
    monkeypatch.setattr(routes, "_turn_subscribers", {})
    monkeypatch.setattr(routes, "_current_user", lambda _r: (1, {"username": "test", "role": "staff"}))
    monkeypatch.setattr(routes, "ensure_access_token", AsyncMock(return_value="mock"))
    monkeypatch.setattr(routes, "connection_status", AsyncMock(return_value={"status": "connected", "connected": True}))
    monkeypatch.setattr(db, "get_llm_key", lambda _uid: {"api_key": "mock", "provider": "mock", "advanced": {}})
    monkeypatch.setattr(routes, "build_user_context", lambda *_a, **_kw: "")
    monkeypatch.setattr(routes.upload_queue, "peek", lambda _uid: [])
    monkeypatch.setattr(routes.upload_queue, "dequeue_all", lambda _uid: [])
    async def run():
        entered, release, created_second = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []
        original_create = db.create_agent_turn
        created = []
        def create_turn(*args):
            row = original_create(*args)
            created.append(row["id"])
            if len(created) == 2:
                created_second.set()
            return row
        monkeypatch.setattr(db, "create_agent_turn", create_turn)
        class FakeAssistant:
            last_tool_receipts = []
            def __init__(self, **_kw):
                pass
            async def chat(self, messages, *_a, **_kw):
                calls.append(messages[-1]["content"])
                if len(calls) == 1:
                    entered.set()
                    await release.wait()
                return "完成"
            async def summarize_memory(self, *_a):
                return ""
        monkeypatch.setattr(routes, "Assistant", FakeAssistant)
        app = FastAPI()
        app.include_router(routes.app_new_router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            first = asyncio.create_task(client.post("/api/app-new/chat", json={"text": "first", "conversation_id": conv}))
            try:
                await asyncio.wait_for(entered.wait(), 5)
            except TimeoutError:
                raise AssertionError([db.get_agent_turn(turn, 1) for turn in created])
            second = asyncio.create_task(client.post("/api/app-new/chat", json={"text": "second", "conversation_id": conv}))
            await asyncio.wait_for(created_second.wait(), 5)
            assert len(calls) == 1
            if cancel_waiting:
                result = await client.post(f"/api/app-new/turns/{created[1]}/cancel")
                assert result.status_code == 200
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(first, second), 5)
            assert all(response.status_code == 200 for response in responses)
        assert len(calls) == (1 if cancel_waiting else 2)
        assert db.get_agent_turn(created[1], 1)["status"] == ("cancelled" if cancel_waiting else "completed")
    asyncio.run(run())
