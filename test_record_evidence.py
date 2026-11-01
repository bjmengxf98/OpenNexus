"""Stable-line evidence contracts; synthetic data and no external services."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent import assistant, record_analyzer as analysis, wps_client as wps
from agent.tool_planner import bounded_record_view, _compact_tool_receipt
from core import business_execution as execution


@pytest.fixture(autouse=True)
def isolated_evidence(monkeypatch):
    analysis._CACHE.clear()
    for name in ("_get", "_post"):
        monkeypatch.setattr(wps, name, AsyncMock(side_effect=AssertionError("no external request")))


def rows():
    return [{"id": f"r{i}", "fields": value} for i, value in enumerate([
        {}, {"k": None}, {"k": ""}, {"k": []}, {"k": {}},
        {"k": 0}, {"k": False}, {"k": "text"},
    ])]


def test_statistics_and_compact_rows_separate_missing_empty_zero_and_false():
    source = rows()
    before = deepcopy(source)
    result = analysis.analyze_record_snapshot(analysis.FetchReceipt(source, 1, True), {
        "fields": ["k"], "group_by": ["k"], "include_rows": True,
    })
    stat = result["field_statistics"]["k"]
    assert (stat["not_returned"], stat["empty"], stat["non_empty"]) == (1, 4, 3)
    assert result["rows"][0][1] == {"_field_state": "not_returned"}
    assert result["rows"][5][1] == "0" and result["rows"][6][1] == "否"
    assert sum(g["count"] for g in result["group_statistics"]) == 8
    assert source == before


@pytest.mark.parametrize("operator", ["empty", "not_empty", "eq", "ne", "contains", "not_contains", "in", "gt"])
def test_missing_filter_input_does_not_prove_match_or_complete_zero(operator):
    result = analysis.analyze_record_snapshot(analysis.FetchReceipt([{"id": "r", "fields": {}}], 1, True), {
        "fields": ["k"], "local_filters": [{"field": "k", "operator": operator, "value": "x"}],
    })
    assert result["matched_records"] == 0 and result["filter_unknown_records"] == 1
    assert result["matched_records_is_exact"] is False and result["is_complete"] is False


@pytest.mark.parametrize("mode,value,count,unknown", [
    ("AND", "no", 0, 0), ("AND", "yes", 0, 1), ("OR", "yes", 1, 0), ("OR", "no", 0, 1),
])
def test_filter_combinations_keep_three_valued_logic(mode, value, count, unknown):
    result = analysis.analyze_cached_records({
        "records": [{"id": "r", "fields": {"known": value}}], "is_complete": True,
    }, {"fields": ["missing", "known"], "local_filter_mode": mode,
        "local_filters": [{"field": "missing", "operator": "ne", "value": "x"},
                          {"field": "known", "operator": "eq", "value": "yes"}]})
    assert "error" not in result
    assert (result["matched_records"], result["filter_unknown_records"]) == (count, unknown)


def test_sparse_records_are_never_whole_empty_rows_or_overwrite_candidates():
    raw = {"records": [{"id": "unknown", "fields": {}}, {"id": "empty", "fields": {"k": None}}],
           "is_complete": True}
    before = deepcopy(raw)
    projected = json.loads(assistant._compress_tool_result("list_records", raw, receipt_index=0)[0])
    assert "empty_row_ids" not in projected and "empty_row_count" not in projected
    assert projected["_field_scope"]["empty_rows_evaluated"] is False
    assert raw == before


def test_partial_window_has_unknown_query_total_and_keeps_scope_after_compaction():
    raw = {"records": [{"id": "r", "fields": {"k": 1}}], "has_more": True,
           "next_page_token": "next", "is_complete": False,
           "_record_scope": {"file_id": "f", "sheet_id": 7, "fields": ["k"], "filter": {"source": "original"}}}
    projected = json.loads(assistant._compress_tool_result("list_records", raw, receipt_index=0)[0])
    assert projected["_completeness"]["total"] is None
    assert not projected["_completeness"]["total_is_known"]
    window, _ = bounded_record_view(projected, 0, fields=["not_returned"], force=True)
    assert window["_record_scope"] == raw["_record_scope"]
    assert window["_record_window"]["visible_fields"] == ["not_returned"]
    compacted = json.loads(_compact_tool_receipt(json.dumps(window)))
    assert compacted["_record_scope"] == raw["_record_scope"]


def test_native_explicit_page_and_tail_keep_actual_cursor_and_total_unknown(monkeypatch):
    calls = []
    data = [{"id": str(i), "fields": json.dumps({"k": i})} for i in range(9)]
    async def post(_token, _path, body):
        calls.append(deepcopy(body))
        start = 5 if body.get("page_token") == "cursor-five" else 0
        end = start + body["page_size"]
        return {"code": 0, "data": {"records": deepcopy(data[start:end]),
                                     "page_token": "cursor-five" if end < len(data) else ""}}
    monkeypatch.setattr(wps, "_post", post)
    query = dict(file_id="f", sheet_id=7, page_size=5, fields=["k"], filter={"mode": "AND", "criteria": []}, view_id="v")
    first = asyncio.run(wps.list_records("token", **query))
    assert len(calls) == 1 and calls[0]["page_size"] == 5
    assert len(first["records"]) == 5 and first["next_page_token"] == "cursor-five"
    assert first["total"] is None and first["total_is_known"] is False
    tail = asyncio.run(wps.list_records("token", **query, page_token=first["next_page_token"]))
    assert tail["page_exhausted"] is True and tail["is_complete"] is False
    assert tail["total"] is None
    assert tail["_record_scope"]["start_token"] == "cursor-five"
    assert tail["_record_scope"]["filter"] == query["filter"]


def test_local_limit_stays_local_and_bounds_the_remote_page(monkeypatch):
    post = AsyncMock(return_value={"code": 0, "data": {"records": [], "page_token": ""}})
    monkeypatch.setattr(wps, "_post", post)
    result = asyncio.run(wps.list_records("token", "f", 7, max_records=3))
    body = post.await_args.args[2]
    assert body["page_size"] == 3 and "max_records" not in body
    assert result["total"] == 0 and result["is_complete"] is True


@pytest.mark.parametrize("parameter,value", [("page_size", 0), ("page_size", -1), ("page_size", True),
    ("page_size", "5"), ("max_records", 0), ("max_records", True), ("page_token", 5)])
def test_invalid_paging_never_sends_a_request(monkeypatch, parameter, value):
    post = AsyncMock()
    monkeypatch.setattr(wps, "_post", post)
    result = asyncio.run(wps.list_records("token", "f", 7, **{parameter: value}))
    assert result["error"] and result["is_complete"] is False
    post.assert_not_awaited()


def test_auto_read_remains_bounded_and_returns_real_continuation(monkeypatch):
    calls = []
    async def post(_token, _path, body):
        calls.append(deepcopy(body))
        n = len(calls)
        return {"code": 0, "data": {"records": [{"id": f"{n}-{i}", "fields": {}} for i in range(1000)],
                                     "page_token": f"next-{n}"}}
    monkeypatch.setattr(wps, "_post", post)
    result = asyncio.run(wps.list_records("token", "f", 7))
    assert len(calls) == 3 and len(result["records"]) == 3000
    assert result["next_page_token"] == "next-3" and not result["is_complete"]
    assert result["total"] is None


def test_failed_page_is_not_complete_zero_or_cacheable(monkeypatch):
    post = AsyncMock(side_effect=[
        {"code": 40000001, "msg": "invalid_request"},
        {"code": 0, "data": {"records": [{"id": "r", "fields": {"k": 1}}], "page_token": ""}},
    ])
    monkeypatch.setattr(wps, "_post", post)
    async def run():
        first = await analysis.analyze_records("token", {"file_id": "f", "sheet_id": 7, "fields": ["k"]})
        assert first["source_is_complete"] is False and first["is_complete"] is False
        second = await analysis.analyze_records("token", {"file_id": "f", "sheet_id": 7, "fields": ["k"]})
        assert second["source_records"] == 1 and second["source_is_complete"] is True
    asyncio.run(run())
    assert post.await_count == 2


def test_cached_analysis_separates_remote_query_local_filter_and_display_window(monkeypatch):
    async def post(_token, _path, _body):
        return {"code": 0, "data": {"records": [{"id": str(i), "fields": {"k": i}} for i in range(3)], "page_token": ""}}
    monkeypatch.setattr(wps, "_post", post)
    remote = {"mode": "AND", "criteria": [{"field": "type", "operator": "Equals", "values": ["x"]}]}
    query = {"file_id": "f", "sheet_id": 7, "fields": ["k"], "filter": remote, "view_id": "v",
             "local_filters": [{"field": "k", "operator": "gt", "value": 0}], "include_rows": True, "row_limit": 1}
    first = asyncio.run(analysis.analyze_records("token", query))
    second = asyncio.run(analysis.analyze_records("token", query))
    assert first == second
    assert first["_record_scope"]["filter"] == remote
    assert first["_analysis_scope"]["source_records"] == 3
    assert first["_analysis_scope"]["matched_records"] == 2 and first["returned_rows"] == 1
    assert first["_analysis_scope"]["local_filters"] == query["local_filters"]


def test_real_chat_loop_reuses_raw_read_receipt_for_local_analysis(monkeypatch, tmp_path):
    from auth import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "evidence-chat.db")
    db.init_db()
    post = AsyncMock(return_value={"code": 0, "data": {
        "records": [{"id": "r", "fields": {"k": 0}}], "page_token": "",
    }})
    monkeypatch.setattr(wps, "_post", post)
    instance = assistant.Assistant.__new__(assistant.Assistant)
    instance.provider, instance.model = "mock", "mock"
    instance.smart_tool_routing, instance.supports_tools = False, True
    instance._auto_learn = AsyncMock()
    step = 0
    async def create(**kwargs):
        nonlocal step
        step += 1
        if step == 1:
            name, args = "list_records", {"file_id": "f", "sheet_id": 7, "fields": ["k"]}
        elif step == 2:
            shown = json.loads(kwargs["messages"][-1]["content"])
            assert shown["_record_scope"]["fields"] == ["k"]
            assert "empty_row_ids" not in shown
            name, args = "analyze_cached_records", {"receipt_index": 0, "fields": ["k"]}
        else:
            shown = json.loads(kwargs["messages"][-1]["content"])
            assert shown["field_statistics"]["k"]["non_empty"] == 1
            assert shown["source_is_complete"] is True
            assert shown["_record_scope"]["file_id"] == "f"
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="已核对已读数据。", tool_calls=None, reasoning_content=None,
            ))])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=None, reasoning_content=None, tool_calls=[SimpleNamespace(
                id=f"call-{step}", function=SimpleNamespace(name=name, arguments=json.dumps(args)),
            )],
        ))])
    instance.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    result = asyncio.run(instance.chat([{"role": "user", "content": "读取并核对数据"}], "token",
                                      default_file={"file_id": "f"}, uid=1))
    assert "已核对" in result
    post.assert_awaited_once()
    raw = instance.last_tool_receipts[0]["result"]
    assert "_completeness" not in raw and "_field_scope" not in raw


def test_mcp_read_preserves_partial_scope_and_structured_failure(monkeypatch):
    from core import mcp_server as service
    monkeypatch.setattr(service, "_wps_access_token", AsyncMock(return_value="token"))
    audit = Mock()
    monkeypatch.setattr(service.db, "add_mcp_audit_log", audit)
    post = AsyncMock(side_effect=[
        {"code": 0, "data": {"records": [{"id": "r", "fields": {}}], "page_token": "next"}},
        {"code": 40000001, "msg": "invalid_request"},
    ])
    monkeypatch.setattr(wps, "_post", post)
    async def run():
        identity = service._identity_var.set({"user_id": 1, "id": 7, "scopes": ["all"]})
        try:
            args = {"file_id": "f", "sheet_id": 7, "page_size": 1, "fields": ["k"]}
            first = await service.execute_tool("list_records", args)
            assert not first["is_complete"] and first["total"] is None
            assert first["_record_scope"]["fields"] == ["k"]
            failed = await service.execute_tool("list_records", args)
            assert failed["ok"] is False and failed["_record_scope"] == first["_record_scope"]
        finally:
            service._identity_var.reset(identity)
    asyncio.run(run())
    assert audit.call_count == 2


@pytest.mark.parametrize("returned,verified", [(True, True), (False, False)])
def test_attachment_shaped_write_readback_requires_actual_field(monkeypatch, returned, verified):
    attachment = [{"name": "报告.docx", "uploadId": "cloud-1", "mode": "cloud", "type": "docx"}]
    post = AsyncMock(return_value={"code": 0, "data": {"records": [{
        "id": "r", "fields": {"附件": deepcopy(attachment)} if returned else {},
    }], "page_token": ""}})
    monkeypatch.setattr(wps, "_post", post)
    write = AsyncMock(return_value={"ok": True})
    result = asyncio.run(execution.execute_record_write("update_records", "token", {
        "file_id": "f", "sheet_id": 7, "records": [{"id": "r", "附件": attachment}],
    }, write=write, read=wps.list_records))
    assert result["verified"] is verified
    write.assert_awaited_once()


def test_delete_absence_only_proven_after_linked_tail_exhausted(monkeypatch):
    post = AsyncMock(side_effect=[
        {"code": 0, "data": {"records": [{"id": "other", "fields": {}}], "page_token": "tail"}},
        {"code": 0, "data": {"records": [], "page_token": ""}},
    ])
    monkeypatch.setattr(wps, "_post", post)
    write = AsyncMock(return_value={"ok": True})
    result = asyncio.run(execution.execute_record_write("delete_records", "token", {
        "file_id": "f", "sheet_id": 7, "record_ids": ["gone"],
    }, write=write, read=wps.list_records))
    assert result["verified"] is True and post.await_count == 2
    assert post.await_args.args[2]["page_token"] == "tail"
    write.assert_awaited_once()


def test_generic_dashboard_joins_native_pages_and_rejects_failed_empty(monkeypatch):
    from core import dashboard_generic_cache as cache
    monkeypatch.setattr(cache, "list_records", wps.list_records)
    post = AsyncMock(side_effect=[
        {"code": 0, "data": {"records": [{"id": "a", "fields": {}}], "page_token": "tail"}},
        {"code": 0, "data": {"records": [{"id": "b", "fields": {}}], "page_token": ""}},
        {"code": 40000001, "msg": "invalid_request"},
    ])
    monkeypatch.setattr(wps, "_post", post)
    result = asyncio.run(cache._load_sheet("token", "f", {"id": 7}))
    assert result["complete"] and result["total"] == 2
    with pytest.raises(RuntimeError, match="有效记录页"):
        asyncio.run(cache._load_sheet("token", "f", {"id": 7}))


def test_reported_more_without_cursor_is_not_complete(monkeypatch):
    monkeypatch.setattr(wps, "_post", AsyncMock(return_value={"data": {
        "records": [{"id": "r", "fields": {}}], "total": 10,
    }}))
    result = asyncio.run(wps.list_records("token", "f", 7, page_size=1))
    assert not result["is_complete"] and result["continuation_error"]


def test_auth_failure_retains_safe_connection_classification(monkeypatch):
    from auth.wps_session import WpsConnectionError
    monkeypatch.setattr(wps, "_post", AsyncMock(side_effect=WpsConnectionError("reauth_required")))
    result = asyncio.run(wps.list_records("token", "f", 7, page_size=1))
    assert result["code"] == "wps_connection_reauth_required"
    assert "重新授权" in result["error"] and not result["is_complete"]


@pytest.mark.parametrize("records", [42, "not-a-page", {}, [{"fields": {}}]])
def test_malformed_page_does_not_become_valid_complete_zero(records):
    from core.record_evidence import normalize_page, record_scope
    result = normalize_page({"records": records}, scope=record_scope(file_id="f", sheet_id=7))
    assert result["error"] and result["is_complete"] is False
    assert result["total"] is None and result["_record_scope"]["file_id"] == "f"
