"""Current-goal and same-turn evidence regression; fake models and WPS only."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent import assistant
from agent.record_analyzer import analyze_cached_records
from agent.tool_planner import compact_tool_messages
from core import record_evidence as evidence
from auth import db


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "goal-repair.db")
    db.init_db()


def page(start, token, values, *, file_id="f", fields=None):
    return evidence.normalize_page({
        "records": [{"id": f"r{i}", "fields": {"kind": value}} for i, value in values],
        "page_token": token,
    }, scope=evidence.record_scope(file_id=file_id, sheet_id=7, fields=fields, start_token=start))


def test_small_read_always_exposes_recoverable_receipt_index_without_raw_mutation():
    raw = page(None, "next", [(1, "a")])
    before = deepcopy(raw)
    projected, _ = assistant._compress_tool_result("list_records", raw, receipt_index=3)
    assert json.loads(projected)["_record_receipt"]["receipt_index"] == 3
    assert raw == before
    messages = [{"role": "tool", "tool_call_id": "x", "content": projected}]
    folded, _ = compact_tool_messages(messages, preserve_recent_tool_messages=0, min_chars=200)
    assert json.loads(folded[0]["content"])["_record_receipt"]["receipt_index"] == 3


def test_linked_partial_pages_can_be_counted_locally_without_claiming_whole_query():
    first = page(None, "next", [(1, "a"), (2, "b")])
    second = page("next", "tail", [(3, "a"), (4, "a")])
    raw = [first, second]
    before = deepcopy(raw)
    merged = evidence.combine_record_pages(raw)
    result = analyze_cached_records(merged, {"fields": ["kind"], "group_by": ["kind"]})
    assert result["source_records"] == 4 and result["source_is_complete"] is False
    assert {row["group"]["kind"]: row["count"] for row in result["group_statistics"]} == {"a": 3, "b": 1}
    assert merged["next_page_token"] == "tail" and merged["total"] is None
    assert raw == before


def test_head_to_exhausted_tail_is_complete_but_tail_alone_is_not():
    head = page(None, "next", [(1, "a")])
    tail = page("next", "", [(2, "b")])
    assert evidence.combine_record_pages([head, tail])["is_complete"] is True
    assert evidence.combine_record_pages([tail])["is_complete"] is False


@pytest.mark.parametrize("change", ["file", "fields", "gap", "reversed", "duplicate", "invalid"])
def test_combination_rejects_unrelated_or_ambiguous_sources(change):
    head = page(None, "next", [(1, "a")])
    tail = page("next", "", [(2, "b")])
    if change == "file":tail["_record_scope"]["file_id"] = "other"
    if change == "fields":tail["_record_scope"]["fields"] = ["kind"]
    if change == "gap":tail["_record_scope"]["start_token"] = "not-next"
    if change == "duplicate":tail["records"][0]["id"] = "r1"
    if change == "invalid":tail["error"] = "failed"
    with pytest.raises(ValueError):
        evidence.combine_record_pages([tail, head] if change == "reversed" else [head, tail])


def make_assistant(create):
    obj = assistant.Assistant.__new__(assistant.Assistant)
    obj.provider, obj.model = "mock", "mock"
    obj.smart_tool_routing, obj.supports_tools = True, True
    obj._auto_learn = AsyncMock()
    obj.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return obj


def response(text=None, name=None, args=None, call="c"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=text, reasoning_content=None,
        tool_calls=[SimpleNamespace(id=call, function=SimpleNamespace(name=name, arguments=json.dumps(args)))] if name else None,
    ))])


def test_chat_small_pages_enable_local_analysis_and_emit_safe_evidence_metadata(monkeypatch):
    read = AsyncMock(side_effect=[page(None, "next", [(1, "a")]), page("next", "tail", [(2, "b")])])
    monkeypatch.setattr(assistant, "list_records", read)
    step = 0
    async def create(**kwargs):
        nonlocal step
        step += 1
        if step == 1:return response(name="list_records", args={"file_id": "f", "sheet_id": 7, "page_size": 1}, call="one")
        if step == 2:
            assert "analyze_cached_records" in {tool["function"]["name"] for tool in kwargs["tools"]}
            shown = json.loads(kwargs["messages"][-1]["content"])
            assert shown["_record_receipt"]["receipt_index"] == 0
            return response(name="list_records", args={"file_id": "f", "sheet_id": 7, "page_size": 1, "page_token": "next"}, call="two")
        if step == 3:return response(name="analyze_cached_records", args={"receipt_indexes": [0, 1], "group_by": ["kind"]}, call="stats")
        result = json.loads(kwargs["messages"][-1]["content"])
        assert result["source_records"] == 2 and len(result["group_statistics"]) == 2
        assert result["_cached_analysis"]["receipt_indexes"] == [0, 1]
        return response("当前两页各类各1条，源范围未读完。")
    obj = make_assistant(create)
    events = []
    async def event(kind, payload):events.append((kind, payload))
    result = asyncio.run(obj.chat([{"role": "user", "content": "读取两页并汇总"}], "secret-token",
                                  default_file={"file_id": "f"}, uid=1, on_agent_event=event))
    assert "未读完" in result and read.await_count == 2
    proof = [value for kind, value in events if kind == "evidence"]
    assert len(proof) == 3 and all(len(item["query_fingerprint"]) == 64 for item in proof)
    encoded = json.dumps(proof)
    assert "secret-token" not in encoded and '"records":' not in encoded and '"next"' not in encoded
    assert all(set(item) == {"tool", "receipt_index", "query_fingerprint", "row_count", "scope_complete", "succeeded", "read_diagnostics"} for item in proof)


@pytest.mark.parametrize("later_lookups", [0, 2])
def test_chat_exposes_conditional_subtotals_from_two_pages_without_extra_remote_reads(monkeypatch, later_lookups):
    states = ["doing", "done", "doing", "done", "done", "doing", "doing", "new", "done", "doing"]
    rows = [{"id": str(i), "fields": {"owner": "B" if i == 0 else "C" if i == 9 else "A", "state": value}}
            for i, value in enumerate(states)]
    receipts = [evidence.normalize_page({"records": rows[:5], "page_token": "next"},
                                       scope=evidence.record_scope(file_id="f", sheet_id=7)),
                evidence.normalize_page({"records": rows[5:], "page_token": "tail"},
                                       scope=evidence.record_scope(file_id="f", sheet_id=7, start_token="next"))]
    original = deepcopy(receipts)
    read = AsyncMock(side_effect=receipts)
    monkeypatch.setattr(assistant, "list_records", read)
    lookup = AsyncMock(return_value={"ok": True, "columns": ["record_id", "name"],
                                    "rows": [["person", "A"]], "source_records": 1,
                                    "matched_records": 1, "source_is_complete": True})
    monkeypatch.setattr(assistant, "analyze_records", lookup)
    step = 0

    async def create(**kwargs):
        nonlocal step
        step += 1
        if step < 3:
            args = {"file_id": "f", "sheet_id": 7, "page_size": 5}
            if step == 2:
                args["page_token"] = "next"
            return response(name="list_records", args=args, call=f"page-{step}")
        if step == 3:
            return response(name="analyze_cached_records",
                            args={"receipt_indexes": [0, 1], "group_by": ["owner", "state"]}, call="subtotal")
        if step < 4 + later_lookups:
            return response(name="analyze_records", args={"file_id": "f", "sheet_id": step}, call=f"lookup-{step}")
        result = json.loads(next(m["content"] for m in kwargs["messages"]
                                 if m.get("tool_call_id") == "subtotal" and m["role"] == "tool"))
        group = next(item for item in result["group_subtotals"] if item["group"]["owner"] == "A")
        counts = {item["value"]: item["count"] for item in group["field_statistics"]["state"]["top_values"]}
        assert counts == {"doing": 3, "done": 4, "new": 1} and group["count"] == 8
        assert result["_statistics_scope"]["counted_records"] == 10
        assert result["source_is_complete"] is False
        return response("A在当前两页有8条：进行中3、已完成4、未开始1；不代表全表。")

    obj = make_assistant(create)
    answer = asyncio.run(obj.chat([{"role": "user", "content": "只汇总连续两页并按主体和状态统计"}],
                                  "mock-token", default_file={"file_id": "f"}, uid=1))
    assert "进行中3、已完成4、未开始1" in answer
    assert read.await_count == 2 and lookup.await_count == later_lookups
    assert step == 4 + later_lookups and receipts == original


@pytest.mark.parametrize("message", [
    "本次只讨论某天工作，只查询分析。先回复收到。",
    "最终改用自然段表达，目前只确认要求，暂不执行。",
    "Acknowledge these constraints and wait for my next instruction.",
])
def test_current_goal_policy_is_visible_without_a_keyword_router_or_extra_model_call(message):
    calls = []
    async def create(**kwargs):
        calls.append(kwargs)
        system = kwargs["messages"][0]["content"]
        assert "当前用户请求决定本轮是否执行" in system
        assert "仅确认" in system and "不得把背景、历史或记忆中的待办自动执行" in system
        assert kwargs.get("tool_choice") != "required"
        return response("收到。")
    obj = make_assistant(create)
    assert asyncio.run(obj.chat([{"role": "user", "content": message}], "", uid=1)) == "收到。"
    assert len(calls) == 1 and obj.last_tool_receipts == []
