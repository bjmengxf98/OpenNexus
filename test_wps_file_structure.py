"""File-type routing contracts; no real WPS or model requests."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import httpx
import pytest

from agent import assistant as assistant_module
from agent import wps_client


def _http_error(status, body=None):
    response = httpx.Response(
        status, json=body or {"code": status, "msg": "request failed"},
        request=httpx.Request("GET", "https://openapi.wps.cn/test"),
    )
    return httpx.HTTPStatusError("request failed", request=response.request, response=response)


@pytest.mark.parametrize("meta,expected", [
    ({"type": "file", "name": "归档台账.XLSX"}, "spreadsheet"),
    ({"name": "台账.et"}, "spreadsheet"),
    ({"name": "台账.xls"}, "spreadsheet"),
    ({"name": "数据.dbt"}, "dbsheet"),
    ({"resource_type": "et", "name": "无后缀"}, "spreadsheet"),
    ({"file_type": "dbt", "name": "无后缀"}, "dbsheet"),
    ({"type": "file", "name": "电子表格"}, None),
    ({"type": "file", "name": "报告.docx"}, None),
])
def test_type_detection_uses_wps_metadata_not_business_names(meta, expected):
    assert wps_client._file_kind_from_metadata(meta) == expected


@pytest.mark.parametrize("kind,filename", [
    ("spreadsheet", "归档台账.xlsx"), ("dbsheet", "部门事务.dbt"),
])
def test_generic_schema_tool_selects_correct_api(monkeypatch, kind, filename):
    meta = AsyncMock(return_value={"type": "file", "name": filename})
    original = {"sheets": [{"sheet_id": 3, "name": "实际工作表"}]}
    db_schema = AsyncMock(return_value=deepcopy(original))
    worksheets = AsyncMock(return_value=deepcopy(original))
    monkeypatch.setattr(wps_client, "get_file_meta", meta)
    monkeypatch.setattr(wps_client, "get_schema", db_schema)
    monkeypatch.setattr(wps_client, "sheets_list_worksheets", worksheets)

    result = asyncio.run(assistant_module.TOOL_MAP["get_schema"](
        "current-user-token", {"file_id": "configured-source"},
    ))

    assert result["file_type"] == kind
    assert result["file_id"] == "configured-source"
    assert result["file_name"] == filename
    assert result["sheets"] == original["sheets"]
    chosen, unused = (worksheets, db_schema) if kind == "spreadsheet" else (db_schema, worksheets)
    chosen.assert_awaited_once_with("current-user-token", "configured-source")
    unused.assert_not_awaited()
    assert result["recommended_tools"] == (
        ["sheets_get_range", "sheets_find_range"] if kind == "spreadsheet"
        else ["list_records", "analyze_records"]
    )


def test_metadata_permission_failure_preserves_existing_schema_access(monkeypatch):
    monkeypatch.setattr(wps_client, "get_file_meta", AsyncMock(side_effect=_http_error(403)))
    schema = AsyncMock(return_value={"sheets": [{"sheet_id": 31, "fields": []}]})
    worksheets = AsyncMock()
    monkeypatch.setattr(wps_client, "get_schema", schema)
    monkeypatch.setattr(wps_client, "sheets_list_worksheets", worksheets)
    result = asyncio.run(wps_client.get_file_structure("token", "same-file"))
    assert result["file_type"] == "dbsheet"
    schema.assert_awaited_once_with("token", "same-file")
    worksheets.assert_not_awaited()


def test_unknown_type_command_not_found_falls_back_once_on_same_file(monkeypatch):
    monkeypatch.setattr(wps_client, "get_file_meta", AsyncMock(return_value={"name": "台账"}))
    schema = AsyncMock(side_effect=_http_error(400, {
        "code": 400000003, "msg": "Command not found",
        "debug": {"type": "E_COMMAND_NOT_FOUND"},
    }))
    worksheets = AsyncMock(return_value={"sheets": [{"sheet_id": 1}]})
    monkeypatch.setattr(wps_client, "get_schema", schema)
    monkeypatch.setattr(wps_client, "sheets_list_worksheets", worksheets)
    result = asyncio.run(wps_client.get_file_structure("token", "same-file"))
    assert result["file_type"] == "spreadsheet"
    schema.assert_awaited_once_with("token", "same-file")
    worksheets.assert_awaited_once_with("token", "same-file")


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
def test_genuine_structure_errors_are_not_type_fallbacks(monkeypatch, status):
    monkeypatch.setattr(wps_client, "get_file_meta", AsyncMock(return_value={"name": "数据.dbt"}))
    schema = AsyncMock(side_effect=_http_error(status, {
        "code": 123, "msg": "Invalid request", "debug": {"type": "InvalidValue"},
    }))
    worksheets = AsyncMock()
    monkeypatch.setattr(wps_client, "get_schema", schema)
    monkeypatch.setattr(wps_client, "sheets_list_worksheets", worksheets)
    result = asyncio.run(wps_client.get_file_structure("token", "same-file"))
    assert result["http_status"] == status
    assert result["wps_code"] == 123
    assert result["wps_error_type"] == "InvalidValue"
    assert result["file_id"] == "same-file"
    assert "不要仅凭 400" in result["_routing_hint"]
    worksheets.assert_not_awaited()


@pytest.mark.parametrize("status", [401, 429, 500])
def test_metadata_auth_or_service_failure_stops_without_probes(monkeypatch, status):
    monkeypatch.setattr(wps_client, "get_file_meta", AsyncMock(side_effect=_http_error(status)))
    schema, worksheets = AsyncMock(), AsyncMock()
    monkeypatch.setattr(wps_client, "get_schema", schema)
    monkeypatch.setattr(wps_client, "sheets_list_worksheets", worksheets)
    result = asyncio.run(wps_client.get_file_structure("token", "file"))
    assert result["http_status"] == status
    assert result["file_type"] == "unknown"
    schema.assert_not_awaited()
    worksheets.assert_not_awaited()


def test_type_probe_is_bounded_even_when_both_interfaces_are_unsupported(monkeypatch):
    error = _http_error(400, {"msg": "Command not found"})
    monkeypatch.setattr(wps_client, "get_file_meta", AsyncMock(return_value={}))
    schema, worksheets = AsyncMock(side_effect=error), AsyncMock(side_effect=error)
    monkeypatch.setattr(wps_client, "get_schema", schema)
    monkeypatch.setattr(wps_client, "sheets_list_worksheets", worksheets)
    result = asyncio.run(wps_client.get_file_structure("token", "file"))
    assert result["http_status"] == 400
    schema.assert_awaited_once()
    worksheets.assert_awaited_once()


def test_raw_dbsheet_schema_contract_stays_unchanged(monkeypatch):
    request = AsyncMock(return_value={"data": {"sheets": [{"id": 31}]}})
    meta = AsyncMock()
    monkeypatch.setattr(wps_client, "_get", request)
    monkeypatch.setattr(wps_client, "get_file_meta", meta)
    result = asyncio.run(wps_client.get_schema("token", "db-file"))
    assert result == {"sheets": [{"id": 31}]}
    request.assert_awaited_once_with("token", "/v7/coop/dbsheet/db-file/schema")
    meta.assert_not_awaited()


class _ScriptedCompletions:
    def __init__(self, steps):
        self.steps, self.calls = steps, []

    async def create(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        index = len(self.calls) - 1
        assert index <= len(self.steps), "Unexpected extra model call"
        if index < len(self.steps):
            name, arguments = self.steps[index]
            calls = [SimpleNamespace(id=f"call-{index}", function=SimpleNamespace(
                name=name, arguments=json.dumps(arguments, ensure_ascii=False),
            ))]
        else:
            calls = None
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=None if calls else "已依据当前台账数据作答", tool_calls=calls,
            reasoning_content=None,
        ))])


def _assistant(completions):
    assistant = assistant_module.Assistant.__new__(assistant_module.Assistant)
    assistant.provider, assistant.model = "mock", "mock-model"
    assistant.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    assistant.supports_tools, assistant.supports_vision = True, False
    assistant.reasoning_mode, assistant.reasoning_effort = "off", "auto"
    assistant.context_window, assistant.max_output_tokens = 0, 1024
    assistant.smart_tool_routing = True
    assistant._auto_learn = AsyncMock()
    return assistant


def _run_query(completions):
    return asyncio.run(_assistant(completions).chat(
        [{"role": "user", "content": "请看看现在的归档工作是什么情况"}],
        access_token="token",
        default_file={"file_id": "archive", "file_name": "归档工作"},
        all_files=[{"file_id": "archive", "file_name": "归档工作"},
                   {"file_id": "department", "file_name": "部门事务"}],
    ))


def test_ordinary_sheet_read_tools_become_visible_from_detected_type(monkeypatch):
    schema = AsyncMock(return_value={
        "file_type": "spreadsheet", "sheets": [{"sheet_id": 3, "name": "台账明细"}],
    })
    cells = AsyncMock(return_value={"range_data": [{"value": "实际归档情况"}]})
    db_read = AsyncMock()
    monkeypatch.setitem(assistant_module.TOOL_MAP, "get_schema", schema)
    monkeypatch.setitem(assistant_module.TOOL_MAP, "sheets_get_range", cells)
    monkeypatch.setitem(assistant_module.TOOL_MAP, "list_records", db_read)
    completions = _ScriptedCompletions([
        ("get_schema", {"file_id": "archive"}),
        ("sheets_get_range", {"file_id": "archive", "worksheet_id": 3,
                              "row_from": 0, "row_to": 30, "col_from": 0, "col_to": 8}),
    ])
    assert _run_query(completions) == "已依据当前台账数据作答"
    initial = {t["function"]["name"] for t in completions.calls[0]["tools"]}
    after_schema = {t["function"]["name"] for t in completions.calls[1]["tools"]}
    assert "get_schema" in initial
    assert {"sheets_get_range", "sheets_find_range"} <= after_schema
    cells.assert_awaited_once()
    assert cells.await_args.args[1]["file_id"] == "archive"
    db_read.assert_not_awaited()
    system = completions.calls[0]["messages"][0]["content"]
    assert "已配置 2 个 WPS 表格" in system
    assert "已配置 2 个多维表格" not in system


@pytest.mark.parametrize("wrong_tool,args", [
    ("list_records", {"sheet_id": 3}),
    ("create_records", {"sheet_id": 3, "records": [{"fields": {"事项": "不可写入"}}]}),
])
def test_verified_spreadsheet_rejects_dbsheet_calls_without_external_side_effects(monkeypatch, wrong_tool, args):
    monkeypatch.setitem(assistant_module.TOOL_MAP, "get_schema", AsyncMock(return_value={
        "file_type": "spreadsheet", "sheets": [{"sheet_id": 3}],
    }))
    incorrect = AsyncMock()
    monkeypatch.setitem(assistant_module.TOOL_MAP, wrong_tool, incorrect)
    completions = _ScriptedCompletions([
        ("get_schema", {"file_id": "archive"}),
        (wrong_tool, {"file_id": "archive", **args}),
    ])
    _run_query(completions)
    incorrect.assert_not_awaited()
    receipts = [m for m in completions.calls[-1]["messages"] if m["role"] == "tool"]
    assert any("file_type_mismatch" in m["content"] for m in receipts)


def test_detected_type_is_per_file_not_global(monkeypatch):
    async def schema(_token, args):
        kind = "spreadsheet" if args["file_id"] == "archive" else "dbsheet"
        return {"file_type": kind, "sheets": [{"sheet_id": 31}]}
    read = AsyncMock(return_value={"records": [], "has_more": False})
    monkeypatch.setitem(assistant_module.TOOL_MAP, "get_schema", schema)
    monkeypatch.setitem(assistant_module.TOOL_MAP, "list_records", read)
    completions = _ScriptedCompletions([
        ("get_schema", {"file_id": "archive"}),
        ("get_schema", {"file_id": "department"}),
        ("list_records", {"file_id": "department", "sheet_id": 0}),
    ])
    _run_query(completions)
    read.assert_awaited_once_with("token", {"file_id": "department", "sheet_id": 31})
