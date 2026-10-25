"""Business parity contracts. All WPS calls and audit writes are simulated."""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest

from agent import assistant, record_analyzer
from core import business_execution as execution
from core import mcp_server as service
from mcp.server.fastmcp.exceptions import ToolError


class WPS:
    def __init__(self, rows=None, *, omit_ids=False, drop_fields=False):
        self.rows = deepcopy(rows or [])
        self.omit_ids = omit_ids
        self.drop_fields = drop_fields
        self.writes = []
        self.reads = []

    async def read(self, token, file_id, sheet_id, **kwargs):
        self.reads.append((token, file_id, sheet_id, kwargs))
        return {"records": deepcopy(self.rows), "has_more": False}

    async def create(self, token, file_id, sheet_id, records):
        self.writes.append((token, file_id, sheet_id, deepcopy(records)))
        added = [{"id": f"new-{i}", "fields": {} if self.drop_fields else record}
                 for i, record in enumerate(records)]
        self.rows.extend(deepcopy(added))
        return {} if self.omit_ids else {"records": list(reversed(deepcopy(added)))}

    async def update(self, token, file_id, sheet_id, records):
        self.writes.append((token, file_id, sheet_id, deepcopy(records)))
        if not self.drop_fields:
            for record in records:
                for row in self.rows:
                    if row["id"] == (record.get("id") or record.get("record_id")):
                        row["fields"].update({k: v for k, v in record.items() if k not in {"id", "record_id"}})
        return {"ok": True}

    async def delete(self, token, file_id, sheet_id, ids):
        self.writes.append((token, file_id, sheet_id, list(ids)))
        if not self.drop_fields:
            self.rows = [row for row in self.rows if row["id"] not in ids]
        return {"ok": True}


@pytest.fixture
def mcp_identity(monkeypatch):
    monkeypatch.setattr(service, "_wps_access_token", AsyncMock(return_value="user-wps-token"))
    monkeypatch.setattr(service.db, "add_mcp_audit_log", Mock())
    context = service._identity_var.set({"user_id": 7, "id": 12, "scopes": ["all"]})
    yield
    service._identity_var.reset(context)


@pytest.mark.parametrize("name,args", [
    ("create_records", {"records": [{"标题": "一"}, {"标题": "二"}]}),
    ("update_records", {"records": [{"id": "old", "标题": "新标题"}]}),
    ("delete_records", {"record_ids": ["old"]}),
])
def test_mcp_and_chat_tool_map_share_write_and_readback(monkeypatch, mcp_identity, name, args):
    arguments = {"file_id": "business-file", "sheet_id": 3, **args}

    async def run():
        results = []
        for remote in (False, True):
            wps = WPS([{"id": "old", "fields": {"标题": "原标题"}}])
            monkeypatch.setattr(assistant, "list_records", wps.read)
            monkeypatch.setattr(assistant, name, getattr(wps, name.split("_")[0]))
            result = (await service.execute_tool(name, arguments) if remote else
                      await assistant.TOOL_MAP[name]("user-wps-token", arguments))
            assert len(wps.writes) == 1
            assert wps.reads and all(row[:3] == ("user-wps-token", "business-file", 3) for row in wps.reads)
            assert result["verified"] is True and result["ok"] is True
            results.append(result)
        assert results[0] == results[1]
    asyncio.run(run())


@pytest.mark.parametrize("uid", [7, 8])
def test_mcp_document_target_uses_only_authenticated_users_configured_files(monkeypatch, mcp_identity, uid):
    monkeypatch.setattr(service.db, "get_default_wps_file", lambda user: {"file_id": f"shared-{user}"})
    monkeypatch.setattr(service.db, "list_wps_files", lambda user: [
        {"file_id": f"shared-{user}"}, {"file_id": f"own-{user}"},
    ])
    generate = AsyncMock(return_value={"ok": True, "cloud_file_id": "saved"})
    monkeypatch.setattr(assistant, "_generate_document", generate)
    arguments = {"title": "报告", "content": "正文", "dbsheet_file_id": "another-users-source",
                 "_upload_anchor_file_ids": ["another-users-storage"]}

    async def run():
        context = service._identity_var.set({"user_id": uid, "id": 12, "scopes": ["all"]})
        try:
            assert (await service.execute_tool("generate_document", arguments))["ok"] is True
        finally:
            service._identity_var.reset(context)
    asyncio.run(run())
    assert generate.await_args.args[3] == f"shared-{uid}"
    assert generate.await_args.args[6] == [f"shared-{uid}", f"own-{uid}"]
    assert arguments["dbsheet_file_id"] == "another-users-source"
    service._wps_access_token.assert_awaited_once_with(uid)


def test_document_without_configured_files_never_calls_wps(monkeypatch, mcp_identity):
    monkeypatch.setattr(service.db, "get_default_wps_file", lambda user: None)
    monkeypatch.setattr(service.db, "list_wps_files", lambda user: [])
    generate = AsyncMock()
    monkeypatch.setattr(assistant, "_generate_document", generate)
    with pytest.raises(ToolError, match="本人创建"):
        asyncio.run(service.execute_tool("generate_document", {"title": "报告", "content": "正文"}))
    generate.assert_not_called()
    service._wps_access_token.assert_not_called()


def test_mcp_catalog_excludes_chat_receipt_tools_and_all_exposed_tools_have_dispatchers():
    async def run():
        catalog = await service.mcp_server.list_tools()
        names = {tool.name for tool in catalog}
        assert not names & service._CHAT_INTERNAL_TOOLS
        assert "analyze_records" in names
        assert {"read_record_window", "analyze_cached_records"} <= {
            tool["function"]["name"] for tool in assistant.TOOLS
        }
        specials = {"save_memory", "send_wecom_message", "send_weixin_message", "get_change_log"}
        extras = {tool["function"]["name"] for tool in service._EXTRA_TOOLS}
        assert names <= set(assistant.TOOL_MAP) | specials | extras
    asyncio.run(run())


@pytest.mark.parametrize("name", ["read_record_window", "analyze_cached_records"])
def test_old_clients_receive_actionable_big_table_replacement(mcp_identity, name):
    with pytest.raises(ToolError, match="analyze_records"):
        asyncio.run(service.execute_tool(name, {"receipt_index": 0}))
    service._wps_access_token.assert_not_called()


def test_mcp_large_table_analysis_supports_compact_continuation(monkeypatch, mcp_identity):
    record_analyzer._CACHE.clear()
    read = AsyncMock(return_value={"records": [
        {"id": f"r-{i}", "fields": {"标题": str(i)}} for i in range(5)
    ], "has_more": False})
    monkeypatch.setattr(record_analyzer, "list_records", read)

    async def run():
        args = {"file_id": "analysis-file", "sheet_id": 1, "fields": ["标题"],
                "include_rows": True, "row_limit": 2}
        first = await service.execute_tool("analyze_records", args)
        second = await service.execute_tool("analyze_records", {**args, "row_offset": first["next_row_offset"]})
        assert first["source_is_complete"] is True and first["has_more"] is True
        assert first["rows"] != second["rows"]
        assert first["matched_records"] == second["matched_records"] == 5
        assert read.await_count == 1
    asyncio.run(run())


def test_create_without_returned_ids_uses_complete_identity_delta_not_existing_similar_row():
    wps = WPS([{"id": "old", "fields": {"标题": "同名"}}], omit_ids=True)
    result = asyncio.run(execution.execute_record_write(
        "create_records", "t", {"file_id": "f", "sheet_id": 1, "records": [{"标题": "同名"}]},
        write=wps.create, read=wps.read))
    assert result["verified"] is True
    assert result["created_ids"] == ["new-0"]
    assert len(wps.writes) == 1 and len(wps.reads) == 2


def test_create_without_ids_does_not_confirm_a_preexisting_row_when_write_is_ignored():
    read = AsyncMock(return_value={"records": [{"id": "old", "fields": {"标题": "同名"}}], "has_more": False})
    write = AsyncMock(return_value={})
    result = asyncio.run(execution.execute_record_write(
        "create_records", "t", {"file_id": "f", "sheet_id": 1, "records": [{"标题": "同名"}]},
        write=write, read=read))
    assert result["verified"] is False and result["write_submitted"] is True
    assert result["verification"]["status"] == "created_ids_ambiguous"
    write.assert_awaited_once()


def test_concurrent_idless_creates_are_not_attributed_to_this_request():
    read = AsyncMock(side_effect=[
        {"records": [], "has_more": False},
        {"records": [{"id": "a", "fields": {"标题": "同名"}},
                     {"id": "b", "fields": {"标题": "同名"}}], "has_more": False},
    ])
    result = asyncio.run(execution.execute_record_write(
        "create_records", "t", {"file_id": "f", "sheet_id": 1, "records": [{"标题": "同名"}]},
        write=AsyncMock(return_value={}), read=read))
    assert result["verified"] is False


@pytest.mark.parametrize("name,args", [
    ("create_records", {"records": [{"标题": "目标"}]}),
    ("update_records", {"records": [{"id": "old", "标题": "目标"}]}),
    ("delete_records", {"record_ids": ["old"]}),
])
def test_silent_ignored_writes_are_never_reported_as_confirmed(name, args):
    wps = WPS([{"id": "old", "fields": {"标题": "原值"}}], drop_fields=True)
    result = asyncio.run(execution.execute_record_write(name, "t", {"file_id": "f", "sheet_id": 1, **args},
                        write=getattr(wps, name.split("_")[0]), read=wps.read))
    assert result["ok"] is False and result["verified"] is False
    assert result["write_submitted"] is True
    assert "不要自动重做" in result["message"]
    assert len(wps.writes) == 1


@pytest.mark.parametrize("page", [
    {"error": "403"}, {"ok": False}, {},
    {"records": [], "has_more": True},
    {"records": [], "has_more": False, "is_complete": False},
    {"records": [{"fields": {"标题": "缺少编号"}}], "has_more": False},
    {"records": [], "code": 400, "has_more": False},
])
def test_failed_or_partial_reads_cannot_prove_deletion(page):
    write = AsyncMock(return_value={"ok": True})
    result = asyncio.run(execution.execute_record_write(
        "delete_records", "t", {"file_id": "f", "sheet_id": 1, "record_ids": ["r"]},
        write=write, read=AsyncMock(return_value=page)))
    assert result["verified"] is False and result["write_submitted"] is True
    write.assert_awaited_once()


def test_verification_follows_pages_and_returns_only_target_rows():
    read = AsyncMock(side_effect=[
        {"records": [{"id": "private-other", "fields": {"标题": "不输出"}}],
         "has_more": True, "next_page_token": "next"},
        {"records": [{"id": "target", "fields": {"标题": "实际值"}}], "has_more": True},
    ])
    result = asyncio.run(execution.execute_record_write(
        "update_records", "t", {"file_id": "f", "sheet_id": 1, "records": [{"id": "target", "标题": "实际值"}]},
        write=AsyncMock(return_value={"ok": True}), read=read))
    assert result["verified"] is True
    assert read.await_args_list[1].kwargs["page_token"] == "next"
    assert read.await_args_list[0].kwargs["fields"] == ["标题"]
    assert "private-other" not in str(result)


def test_bounded_verification_does_not_treat_partial_absence_as_deletion(monkeypatch):
    monkeypatch.setattr(execution, "_VERIFY_MAX_PAGES", 2)
    read = AsyncMock(side_effect=[
        {"records": [], "has_more": True, "next_page_token": "p1"},
        {"records": [], "has_more": True, "next_page_token": "p2"},
    ])
    result = asyncio.run(execution.execute_record_write(
        "delete_records", "t", {"file_id": "f", "sheet_id": 1, "record_ids": ["r"]},
        write=AsyncMock(return_value={"ok": True}), read=read))
    assert not result["verified"] and read.await_count == 2


def test_transport_failure_is_unknown_not_a_safe_write_retry():
    write = AsyncMock(side_effect=TimeoutError("server may have committed"))
    read = AsyncMock()
    result = asyncio.run(execution.execute_record_write(
        "update_records", "t", {"file_id": "f", "sheet_id": 1, "records": [{"id": "r", "标题": "值"}]},
        write=write, read=read))
    assert result["write_submitted"] is None and result["verified"] is False
    assert "不要自动重做" in result["message"]
    write.assert_awaited_once()
    read.assert_not_called()


def test_readback_timeout_is_bounded_and_does_not_repeat_write(monkeypatch):
    monkeypatch.setattr(execution, "_VERIFY_TIMEOUT_SECONDS", 0.01)
    write = AsyncMock(return_value={"ok": True})

    async def blocked_read(*args, **kwargs):
        await asyncio.sleep(1)
    result = asyncio.run(execution.execute_record_write(
        "update_records", "t", {"file_id": "f", "sheet_id": 1, "records": [{"id": "r", "标题": "值"}]},
        write=write, read=blocked_read))
    assert result["verification"]["status"] == "readback_unavailable"
    assert result["write_submitted"] is True
    write.assert_awaited_once()


def test_unverified_mcp_write_is_audited_as_not_confirmed(monkeypatch, mcp_identity):
    monkeypatch.setattr(assistant, "update_records", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(assistant, "list_records", AsyncMock(return_value={
        "records": [{"id": "r", "fields": {"标题": "原值"}}], "has_more": False,
    }))
    result = asyncio.run(service.execute_tool("update_records", {
        "file_id": "f", "sheet_id": 1, "records": [{"id": "r", "标题": "新值"}],
    }))
    assert result["ok"] is False
    assert service.db.add_mcp_audit_log.call_args.args[4] is False


def test_new_verification_receipts_preserve_internal_id_display_boundary():
    receipts = [{"name": "create_records", "result": {
        "created_ids": ["new-task"], "verification": {"target_ids": ["new-task"],
        "observed_records": [{"id": "new-task", "fields": {"标题": "任务"}}]},
    }}]
    assert "new-task" not in assistant._sanitize_user_visible_wps_ids("任务已建立，记录 ID：`new-task`", receipts)
    assert "new-task" in assistant._sanitize_user_visible_wps_ids("`new-task`", receipts, "给我记录ID")


@pytest.mark.parametrize("name,args", [
    ("create_records", {"records": []}), ("create_records", {"records": [{}]}),
    ("update_records", {"records": [{"标题": "值"}]}),
    ("update_records", {"records": [{"id": "r"}]}),
    ("update_records", {"records": [{"id": "r", "标题": "a"}, {"id": "r", "标题": "b"}]}),
    ("delete_records", {"record_ids": [""]}),
])
def test_invalid_record_arguments_cause_zero_reads_or_writes(name, args):
    write, read = AsyncMock(), AsyncMock()
    result = asyncio.run(execution.execute_record_write(name, "t", {"file_id": "f", "sheet_id": 1, **args},
                                                       write=write, read=read))
    assert result["write_submitted"] is False
    write.assert_not_called()
    read.assert_not_called()


def test_contact_verification_uses_stable_identity_and_checks_all_fields():
    expected = {"标题": "任务", "执行人": [{"id": "wps-1", "nickName": "旧昵称"}]}
    actual = {"id": "r", "fields": {"标题": "任务", "执行人": [{"id": "wps-1", "nickName": "新昵称"}]}}
    assert execution.record_values_match(expected, actual)
    actual["fields"]["执行人"][0]["id"] = "wps-2"
    assert not execution.record_values_match(expected, actual)
    assert not execution.record_values_match({"缺失字段": "值"}, actual)


def test_record_cache_invalidation_is_table_scoped_and_blocks_inflight_stale_put(monkeypatch):
    record_analyzer._CACHE.clear()
    receipt = record_analyzer.FetchReceipt([], 1, True)
    keys = [record_analyzer._cache_key(t, f, 1, None, None, None)
            for t, f in [("a", "f"), ("b", "f"), ("a", "other")]]
    for key in keys:
        record_analyzer._cache_put(key, receipt)
    record_analyzer.invalidate_record_cache("f", 1)
    assert all(record_analyzer._cache_get(key) is None for key in keys[:2])
    assert record_analyzer._cache_get(keys[2]) is not None

    async def racing_read(*args, **kwargs):
        record_analyzer.invalidate_record_cache("race", 1)
        return {"records": [], "has_more": False}
    monkeypatch.setattr(record_analyzer, "list_records", racing_read)
    asyncio.run(record_analyzer._fetch_all_records("a", "race", 1, fields=None, server_filter=None, view_id=None))
    key = record_analyzer._cache_key("a", "race", 1, None, None, None)
    assert record_analyzer._cache_get(key) is None
