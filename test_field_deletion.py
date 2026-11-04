"""WPS field deletion protocol and readback contracts; no live WPS/model calls."""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from agent import assistant, record_analyzer, wps_client
from core import mcp_server
from mcp.server.fastmcp.exceptions import ToolError


def schema(*ids, sheet_id=3):
    return {"sheets": [{"id": sheet_id, "fields": [{"id": fid} for fid in ids]}]}


def receipt(*ids):
    return {"code": 0, "data": {"fields": [{"id": fid, "deleted": True} for fid in ids]}}


@pytest.fixture
def upstream(monkeypatch):
    post = AsyncMock(return_value=receipt("B"))
    read = AsyncMock(return_value=schema("A"))
    invalidate = Mock()
    monkeypatch.setattr(wps_client, "_post", post)
    monkeypatch.setattr(wps_client, "get_schema", read)
    monkeypatch.setattr(record_analyzer, "invalidate_record_cache", invalidate)
    return post, read, invalidate


def delete(ids=None):
    return asyncio.run(wps_client.delete_fields("caller-token", "file", 3, ["B"] if ids is None else ids))


def test_correct_signed_payload_and_same_user_file_readback(upstream):
    post, read, invalidate = upstream
    original = ["B"]
    result = delete(original)
    post.assert_awaited_once_with("caller-token", "/v7/coop/dbsheet/file/sheets/3/fields/delete",
                                  {"fields": ["B"]}, signed=True)
    read.assert_awaited_once_with("caller-token", "file")
    invalidate.assert_called_once_with("file", 3)
    assert original == ["B"]
    assert result["verified"] is True and result["deleted_ids"] == ["B"]


@pytest.mark.parametrize("ids", [[], "B", [""], [" "], [None], [3], ["B", "B"]])
def test_invalid_inputs_do_not_write_or_read(upstream, ids):
    post, read, invalidate = upstream
    result = delete(ids)
    assert result["write_submitted"] is False
    post.assert_not_awaited()
    read.assert_not_awaited()
    invalidate.assert_not_called()


@pytest.mark.parametrize("body", [
    {"code": 0, "data": {}}, {"code": 0, "data": {"fields": []}},
    {"code": 0, "data": {"fields": [{"id": "B", "deleted": False}]}},
    {"code": 0, "data": {"fields": [{"id": "B", "deleted": "true"}]}},
    receipt("C"), receipt("B", "B"),
])
def test_empty_failed_unrelated_or_ambiguous_ack_is_not_success(upstream, body):
    post, read, _ = upstream
    post.return_value = body
    # Even an absent ID cannot turn a missing acknowledgement into success.
    read.return_value = schema("A")
    result = delete()
    assert result["ok"] is False and result["verified"] is False
    assert result["deleted_ids"] == []
    assert post.await_count == 1


def test_reported_success_but_field_still_present_is_unverified(upstream):
    post, read, _ = upstream
    read.return_value = schema("A", "B")
    result = delete()
    assert result["api_deleted_ids"] == ["B"]
    assert result["verified"] is False
    assert result["verification"]["remaining_ids"] == ["B"]
    assert post.await_count == 1


@pytest.mark.parametrize("value", [
    {}, {"sheets": []}, schema("A", sheet_id=4),
    {"sheets": [{"id": 3}]}, {"sheets": [{"id": 3, "fields": None}]},
    {"sheets": [{"id": 3, "fields": [{"name": "名称"}]}]},
    {"sheets": [{"id": 3, "fields": [{"id": "A"}, {"id": "A"}]}]},
    {**schema("A"), "has_more": True}, {**schema("A"), "is_complete": False},
    {**schema("A"), "code": 403000001},
])
def test_missing_wrong_or_partial_schema_cannot_prove_deletion(upstream, value):
    post, read, _ = upstream
    read.return_value = value
    result = delete()
    assert result["verified"] is False
    assert result["verification"]["status"] == "unknown"
    assert post.await_count == 1


def test_partial_delete_keeps_individual_results_without_replaying(upstream):
    post, read, _ = upstream
    body = receipt("B")
    body["data"]["fields"].append({"id": "C", "deleted": False})
    post.return_value = deepcopy(body)
    read.return_value = schema("A", "C")
    result = delete(["B", "C"])
    assert result["ok"] is False and result["verification"]["status"] == "partial"
    assert result["deleted_ids"] == ["B"]
    assert result["fields"] == body["data"]["fields"]
    assert result["verification"]["unconfirmed_ids"] == ["C"]
    assert post.await_count == 1


def test_api_error_code_not_hidden_by_empty_data(upstream):
    post, read, _ = upstream
    post.return_value = {"code": 403000001, "msg": "permission denied", "data": {}}
    result = delete()
    assert "403000001" in result["error"] and result["detail"]["code"] == 403000001
    assert result["verified"] is False
    read.assert_not_awaited()


def test_transport_failure_does_not_replay_and_invalidates_cache(upstream):
    post, read, invalidate = upstream
    post.side_effect = TimeoutError("simulated unknown commit")
    result = delete()
    assert result["write_submitted"] is None and result["verified"] is False
    assert result["verification"]["status"] == "submission_unknown"
    assert post.await_count == 1
    invalidate.assert_called_once_with("file", 3)
    read.assert_not_awaited()


def test_readback_failure_does_not_repeat_delete(upstream):
    post, read, _ = upstream
    read.side_effect = RuntimeError("simulated read failure")
    result = delete()
    assert result["write_submitted"] is True and result["verified"] is False
    assert post.await_count == 1


def test_repeated_explicit_delete_reports_already_absent_ack_as_unconfirmed(upstream):
    post, _, _ = upstream
    assert delete()["verified"] is True
    post.return_value = {"code": 0, "data": {"fields": [{"id": "B", "deleted": False}]}}
    assert delete()["verified"] is False
    assert post.await_count == 2  # Two user calls, one mutation each; no internal replay.


def test_actual_transport_includes_kso_headers(monkeypatch):
    requests = []

    def handle(request):
        import json
        requests.append(request)
        assert json.loads(request.content) == {"fields": ["B"]}
        assert request.headers["authorization"] == "Bearer caller-token"
        assert request.headers["x-kso-authorization"].startswith("KSO-1 test-app:")
        assert request.headers["x-kso-date"]
        return httpx.Response(200, json=receipt("B"))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(wps_client.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(wps_client, "request_token", AsyncMock(side_effect=lambda t: t))
    monkeypatch.setattr(wps_client, "validate_request_token", lambda t: t)
    monkeypatch.setattr(wps_client, "get_app_id", lambda: "test-app")
    monkeypatch.setattr(wps_client, "get_app_secret", lambda: "test-secret")
    monkeypatch.setattr(wps_client, "get_schema", AsyncMock(return_value=schema("A")))
    assert delete()["verified"] is True
    assert len(requests) == 1


@pytest.mark.parametrize("scopes,allowed", [
    (["manage_structure", "delete_data"], True), (["manage_structure"], False), (["delete_data"], False),
])
def test_chat_and_mcp_share_fix_without_expanding_token_grants(monkeypatch, upstream, scopes, allowed):
    post, _, _ = upstream
    monkeypatch.setattr(mcp_server, "_wps_access_token", AsyncMock(return_value="caller-token"))
    monkeypatch.setattr(mcp_server.db, "add_mcp_audit_log", Mock())
    args = {"file_id": "file", "sheet_id": 3, "field_ids": ["B"]}
    marker = mcp_server._identity_var.set({"user_id": 7, "id": 12, "scopes": scopes})
    try:
        if allowed:
            local = asyncio.run(assistant.TOOL_MAP["delete_fields"]("caller-token", args))
            remote = asyncio.run(mcp_server.execute_tool("delete_fields", args))
            assert local == remote and remote["verified"] is True
            assert post.await_count == 2
        else:
            with pytest.raises(ToolError):
                asyncio.run(mcp_server.execute_tool("delete_fields", args))
            post.assert_not_awaited()
            mcp_server._wps_access_token.assert_not_awaited()
    finally:
        mcp_server._identity_var.reset(marker)
