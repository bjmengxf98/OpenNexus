import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent import wps_client
from agent.assistant import Assistant
from agent.tool_planner import (
    TaskPlanState,
    discover_tool_names,
    initial_tool_names,
    is_complex_task,
    trim_history_to_budget,
)


def _tool(name, description=""):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_initial_routing_keeps_full_data_tools_but_not_unrelated_channels():
    selected = initial_tool_names("请分析并总结今年全部工作记录")
    assert is_complex_task("请分析并总结今年全部工作记录")
    assert {"get_schema", "list_records"} <= selected
    assert "analyze_records" in selected
    assert "send_weixin_message" not in selected
    assert "generate_document" not in selected


def test_discovery_falls_back_to_all_tools_when_no_match():
    tools = [
        _tool("alpha_tool", "alpha capability"),
        _tool("beta_tool", "beta capability"),
    ]
    names, fallback_all = discover_tool_names("完全未知的业务能力", tools)
    assert names == ["alpha_tool", "beta_tool"]
    assert fallback_all is True


def test_task_plan_cannot_claim_completion_with_unfinished_steps():
    state = TaskPlanState()
    with pytest.raises(ValueError):
        state.update({
            "goal": "总结全年工作",
            "status": "completed",
            "steps": [{"content": "读取全部记录", "status": "in_progress"}],
            "completion_criteria": ["所有分页均读取完成"],
        })

    result = state.update({
        "goal": "总结全年工作",
        "status": "in_progress",
        "steps": [{"content": "读取全部记录", "status": "in_progress"}],
        "completion_criteria": ["所有分页均读取完成"],
        "data_scope": {"range": "全年", "all_pages": True},
    })
    assert result["status"] == "in_progress"
    assert result["unfinished"] == ["读取全部记录"]


def test_history_budget_preserves_latest_user_message():
    history = []
    for index in range(20):
        history.append({"role": "user", "content": f"旧问题{index}" + "数" * 500})
        history.append({"role": "assistant", "content": f"旧回答{index}" + "据" * 500})
    history.append({"role": "user", "content": "必须保留的当前任务"})
    trimmed = trim_history_to_budget(
        history,
        system_prompt="系统规则",
        tools=[_tool("list_records", "查询记录")],
        context_window=4096,
        max_output_tokens=512,
    )
    assert len(trimmed) < len(history)
    assert trimmed[-1]["content"] == "必须保留的当前任务"
    assert trimmed[0]["role"] == "user"


def test_list_records_returns_cursor_when_auto_fetch_hits_safety_window(monkeypatch):
    responses = [
        {
            "data": {
                "records": [{"id": f"r{page}-{row}", "fields": {}} for row in range(1000)],
                "total": 4000,
                "has_more": True,
                "next_page_token": f"p{page + 1}",
            }
        }
        for page in range(1, 4)
    ]

    async def fake_post(_token, _path, body):
        assert body.get("page_token") in {None, "p2", "p3"}
        return responses.pop(0)

    monkeypatch.setattr(wps_client, "_post", fake_post)
    result = asyncio.run(wps_client.list_records("token", "file", 1))

    assert result["fetched"] == 3000
    assert result["has_more"] is True
    assert result["is_complete"] is False
    assert result["next_page_token"] == "p4"
    assert result["continuation_available"] is True


class _LongPlanCompletions:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        index = len(self.calls) - 1
        if index <= 50:
            completed = index == 50
            arguments = {
                "goal": "验证超过50轮仍可按目标完成",
                "status": "completed" if completed else "in_progress",
                "steps": [{
                    "content": f"验证步骤{index}",
                    "status": "completed" if completed else "in_progress",
                }],
                "completion_criteria": ["模型完成第51次有效计划更新"],
                "data_scope": {"round": index},
            }
            tool_call = SimpleNamespace(
                id=f"plan-{index}",
                function=SimpleNamespace(
                    name="update_task_plan",
                    arguments=json.dumps(arguments, ensure_ascii=False),
                ),
            )
            message = SimpleNamespace(
                content=None,
                tool_calls=[tool_call],
                reasoning_content=None,
            )
        else:
            message = SimpleNamespace(
                content="超过50轮后按完成条件正常结束",
                tool_calls=None,
                reasoning_content=None,
            )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_goal_driven_loop_can_finish_after_more_than_fifty_tool_rounds():
    completions = _LongPlanCompletions()
    assistant = Assistant.__new__(Assistant)
    assistant.provider = "mock"
    assistant.model = "mock-model"
    assistant.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    assistant.supports_tools = True
    assistant.supports_vision = False
    assistant.reasoning_mode = "off"
    assistant.reasoning_effort = "auto"
    assistant.context_window = 0
    assistant.max_output_tokens = 1024
    assistant.smart_tool_routing = True
    assistant._auto_learn = AsyncMock()

    reply = asyncio.run(assistant.chat(
        [{"role": "user", "content": "请分析今年全部数据并完成验证"}],
        access_token="",
    ))

    assert reply == "超过50轮后按完成条件正常结束"
    assert len(completions.calls) == 52
    first_tool_names = {
        item["function"]["name"] for item in completions.calls[0]["tools"]
    }
    assert "update_task_plan" in first_tool_names
    assert len(first_tool_names) < 20


def test_local_analyzer_reads_all_pages_and_returns_compact_windows(monkeypatch):
    from agent import record_analyzer

    record_analyzer._CACHE.clear()
    calls = []

    async def fake_list_records(
        _token, _file_id, _sheet_id, page_size=1000, page_token=None,
        fields=None, filter=None, view_id=None, **_kwargs,
    ):
        calls.append(page_token)
        if page_token is None:
            return {
                "records": [
                    {"id": "r1", "fields": {"部门": "一部", "工作": "完成A", "数量": 2}},
                    {"id": "r2", "fields": {"部门": "二部", "工作": "完成B", "数量": 7}},
                ],
                "has_more": True,
                "next_page_token": "p2",
            }
        return {
            "records": [
                {"id": "r3", "fields": {"部门": "一部", "工作": "完成C", "数量": 3}},
            ],
            "has_more": False,
            "next_page_token": None,
        }

    monkeypatch.setattr(record_analyzer, "list_records", fake_list_records)
    arguments = {
        "file_id": "file",
        "sheet_id": 1,
        "fields": ["部门", "工作", "数量"],
        "local_filters": [{"field": "部门", "operator": "eq", "value": "一部"}],
        "group_by": ["部门"],
        "numeric_fields": ["数量"],
        "include_rows": True,
        "row_limit": 1,
    }
    first = asyncio.run(record_analyzer.analyze_records("token", arguments))
    assert first["source_records"] == 3
    assert first["matched_records"] == 2
    assert first["pages_fetched"] == 2
    assert first["source_is_complete"] is True
    assert first["returned_rows"] == 1
    assert first["has_more"] is True
    assert first["next_row_offset"] == 1
    assert first["group_statistics"][0]["count"] == 2
    assert first["field_statistics"]["数量"]["numeric"]["sum"] == 5.0

    second = asyncio.run(record_analyzer.analyze_records(
        "token", {**arguments, "row_offset": first["next_row_offset"]}
    ))
    assert second["rows"][0][0] == "r3"
    assert second["is_complete"] is True
    assert calls == [None, "p2"]


def test_compaction_preserves_recent_tool_result_and_receipts_old_result():
    from agent.tool_planner import compact_tool_messages

    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "summarize"},
        {"role": "tool", "tool_call_id": "old", "content": json.dumps({
            "records": [{"id": str(i), "fields": {"工作": "x" * 100}} for i in range(40)],
            "total": 40,
            "fetched": 40,
            "is_complete": True,
        }, ensure_ascii=False)},
        {"role": "tool", "tool_call_id": "new", "content": "latest result"},
    ]
    compacted, count = compact_tool_messages(
        messages, preserve_recent_tool_messages=1, min_chars=1000,
    )
    assert count == 1
    assert json.loads(compacted[2]["content"])["_compacted"] is True
    assert compacted[3]["content"] == "latest result"


def test_run_metrics_prefers_provider_usage_when_available():
    from agent.tool_planner import RunMetrics

    response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30),
        choices=[SimpleNamespace(message=SimpleNamespace(content="完成", tool_calls=None))],
    )
    metrics = RunMetrics(full_tool_count=52, full_tool_tokens=9000)
    metrics.tool_calls = 2
    metrics.record_response(
        response,
        messages=[{"role": "user", "content": "任务"}],
        tools=[_tool("analyze_records")],
    )
    snapshot = metrics.snapshot()
    assert snapshot["total_tokens"] == 150
    assert snapshot["estimated"] is False
    assert snapshot["tool_calls"] == 2
    assert snapshot["tool_definition_saving_rate"] > 0


class _OverflowThenSuccess:
    def __init__(self):
        self.calls = 0

    async def create(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("Input tokens exceed the configured limit")
        message = SimpleNamespace(
            content="自动降载后完成",
            tool_calls=None,
            reasoning_content=None,
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_context_limit_is_compacted_and_retried_without_false_completion():
    completions = _OverflowThenSuccess()
    assistant = Assistant.__new__(Assistant)
    assistant.provider = "mock"
    assistant.model = "mock-model"
    assistant.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    assistant.supports_tools = True
    assistant.supports_vision = False
    assistant.reasoning_mode = "off"
    assistant.reasoning_effort = "auto"
    assistant.context_window = 192000
    assistant.max_output_tokens = 1024
    assistant.smart_tool_routing = True
    assistant._auto_learn = AsyncMock()
    events = []

    async def on_event(kind, payload):
        events.append((kind, payload))

    reply = asyncio.run(assistant.chat(
        [{"role": "user", "content": "请分析今年全部工作"}],
        access_token="",
        on_agent_event=on_event,
    ))
    assert reply == "自动降载后完成"
    assert completions.calls == 2
    assert assistant.last_run_metrics["context_recoveries"] == 1
    assert any(kind == "context" and payload["status"] == "recovered"
               for kind, payload in events)



def test_task_plan_keeps_findings_when_later_update_omits_them():
    from agent.tool_planner import TaskPlanState

    plan = TaskPlanState()
    base = {
        "goal": "总结全年工作",
        "status": "in_progress",
        "steps": [
            {"content": "读取数据", "status": "completed"},
            {"content": "形成总结", "status": "in_progress"},
        ],
        "completion_criteria": ["全年记录已完整读取", "总结已形成"],
    }
    plan.update({**base, "findings": ["全年共有120项工作"]})
    plan.update({
        **base,
        "steps": [
            {"content": "读取数据", "status": "completed"},
            {"content": "形成总结", "status": "completed"},
        ],
        "status": "completed",
    })
    assert plan.findings == ["全年共有120项工作"]


def test_local_filter_mixed_types_do_not_abort_analysis(monkeypatch):
    from agent import record_analyzer

    record_analyzer._CACHE.clear()

    async def fake_list_records(*_args, **_kwargs):
        return {
            "records": [
                {"id": "r1", "fields": {"数量": "待定"}},
                {"id": "r2", "fields": {"数量": 8}},
            ],
            "has_more": False,
        }

    monkeypatch.setattr(record_analyzer, "list_records", fake_list_records)
    result = asyncio.run(record_analyzer.analyze_records("mixed-token", {
        "file_id": "file",
        "sheet_id": 1,
        "local_filters": [{"field": "数量", "operator": "gte", "value": 5}],
        "include_rows": True,
    }))
    assert result["matched_records"] == 1
    assert result["rows"][0][0] == "r2"


class _CaptureToolsOnce:
    def __init__(self):
        self.request = None

    async def create(self, **kwargs):
        self.request = kwargs
        message = SimpleNamespace(
            content="当前表格已确认",
            tool_calls=None,
            reasoning_content=None,
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10),
        )


def _tool_count_for_mode(smart_tool_routing: bool) -> int:
    completions = _CaptureToolsOnce()
    assistant = Assistant.__new__(Assistant)
    assistant.provider = "mock"
    assistant.model = "mock-model"
    assistant.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    assistant.supports_tools = True
    assistant.supports_vision = False
    assistant.reasoning_mode = "off"
    assistant.reasoning_effort = "auto"
    assistant.context_window = 0
    assistant.max_output_tokens = 1024
    assistant.smart_tool_routing = smart_tool_routing
    assistant._auto_learn = AsyncMock()

    reply = asyncio.run(assistant.chat(
        [{"role": "user", "content": "当前是哪个表格"}],
        access_token="",
    ))
    assert reply == "当前表格已确认"
    return len(completions.request.get("tools") or [])


def test_all_tools_mode_really_sends_every_tool_while_smart_mode_routes():
    from agent.assistant import TOOLS

    smart_count = _tool_count_for_mode(True)
    all_count = _tool_count_for_mode(False)

    assert smart_count == 5
    assert all_count == len(TOOLS) == 52
    assert all_count > smart_count
