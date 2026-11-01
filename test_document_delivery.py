"""Document delivery uses private temporary storage and simulated WPS calls only."""
import asyncio
import json
import re
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from auth import db
from agent import wps_client as wps
from core import document_generator as generator


CONTENT = {"sections": [{"type": "paragraph", "text": "需要保留的报告正文。"}]}
REAL_UPLOAD = wps.upload_to_drive


@pytest.fixture(autouse=True)
def private_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(wps, "upload_to_drive", AsyncMock(return_value={"uploadId": "cloud-1"}))


def generate(**kwargs):
    return asyncio.run(generator.generate_and_upload_document(
        "token", "报告", CONTENT, "own-file", user_id=1, **kwargs,
    ))


def resume(document_id, uid=1, token="new-token"):
    return asyncio.run(generator.generate_and_upload_document(
        token, document_id=document_id, dbsheet_file_id="own-file", user_id=uid,
    ))


def rejected(status=403):
    response = httpx.Response(status, request=httpx.Request("POST", "https://wps.test/upload"))
    return httpx.HTTPStatusError("private-token-must-not-leak", request=response.request, response=response)


def test_failed_upload_keeps_original_document_and_retry_does_not_render_again(monkeypatch):
    wps.upload_to_drive.side_effect = rejected()
    first = generate()
    assert first["generated"] is True and first["uploaded"] is False
    assert first["delivery_status"] == "upload_failed" and first["retryable"] is True
    assert first["document_id"] and first["retained"] is True
    original = wps.upload_to_drive.await_args.args[3]
    assert "private-token" not in json.dumps(first)
    assert str(db.DB_PATH.parent) not in json.dumps(first)

    monkeypatch.setattr(generator, "_apply_document_template", lambda *_a: pytest.fail("must reuse DOCX"))
    wps.upload_to_drive.side_effect = None
    second = resume(first["document_id"])
    assert second["ok"] is True and second["delivery_status"] == "uploaded"
    assert second["file_name"] == first["file_name"]
    assert wps.upload_to_drive.await_args.args[3] == original
    assert wps.upload_to_drive.await_args.args[0] == "new-token"


def test_successful_document_receipt_is_idempotent_without_uploading_again():
    first = generate()
    assert not list((db.DB_PATH.parent / "generated_documents" / "1").glob("*.docx"))
    second = resume(first["document_id"])
    assert second["ok"] is True and second["cloud_file_id"] == "cloud-1"
    assert second["document_id"] == first["document_id"]
    wps.upload_to_drive.assert_awaited_once()


@pytest.mark.parametrize("uid,reference", [(2, None), (1, "../other.docx"), (0, None)])
def test_document_reference_never_bypasses_owner_or_path_boundaries(uid, reference):
    first = generate()
    before = wps.upload_to_drive.await_count
    result = resume(reference or first["document_id"], uid)
    assert result["ok"] is False
    assert wps.upload_to_drive.await_count == before
    assert "cloud-1" not in json.dumps(result)


def test_no_wps_token_still_preserves_generated_document():
    first = asyncio.run(generator.generate_and_upload_document(
        "", "报告", CONTENT, "own-file", user_id=1,
    ))
    assert first["generated"] and first["retained"]
    assert first["uploaded"] is False and first["retryable"] is True
    wps.upload_to_drive.assert_not_awaited()
    assert resume(first["document_id"])["ok"] is True


def test_commit_timeout_is_unknown_and_never_blindly_replayed():
    async def upload(*_args, on_phase, **_kwargs):
        on_phase("prepared", {"drive_id": "personal-drive", "parent_id": "ai-folder"})
        on_phase("commit_started", {})
        raise httpx.ReadTimeout("secret-upstream-detail")

    wps.upload_to_drive.side_effect = upload
    first = generate()
    assert first["delivery_status"] == "upload_unknown" and first["uploaded"] is None
    assert first["generated"] and first["retained"] and first["retryable"] is False
    assert "secret-upstream" not in json.dumps(first)
    assert resume(first["document_id"])["delivery_status"] == "upload_unknown"
    wps.upload_to_drive.assert_awaited_once()


def test_same_pending_content_cannot_bypass_unknown_state_by_omitting_reference(monkeypatch):
    async def upload(*_args, on_phase, **_kwargs):
        on_phase("commit_started", {})
        raise httpx.ReadTimeout("commit outcome unknown")
    wps.upload_to_drive.side_effect = upload
    first = generate()
    monkeypatch.setattr(generator, "_apply_document_template", lambda *_a: pytest.fail("pending content must be reused"))
    repeated = generate()
    assert repeated["document_id"] == first["document_id"]
    assert repeated["delivery_status"] == "upload_unknown"
    assert repeated["retryable"] is False
    wps.upload_to_drive.assert_awaited_once()


def test_definite_commit_rejection_can_retry_saved_document():
    async def upload(*_args, on_phase, **_kwargs):
        on_phase("prepared", {"drive_id": "personal-drive", "parent_id": "ai-folder"})
        on_phase("commit_started", {})
        raise rejected(401)

    wps.upload_to_drive.side_effect = upload
    first = generate()
    assert first["delivery_status"] == "upload_failed" and first["retryable"] is True
    wps.upload_to_drive.side_effect = None
    assert resume(first["document_id"])["ok"] is True


def test_concurrent_resume_submits_saved_document_only_once():
    async def run():
        first = await generator.generate_and_upload_document("", "报告", CONTENT, "own-file", user_id=1)
        started, finish = asyncio.Event(), asyncio.Event()

        async def upload(*_args, on_phase, **_kwargs):
            started.set()
            await finish.wait()
            on_phase("commit_started", {})
            return {"uploadId": "cloud-1"}

        wps.upload_to_drive.side_effect = upload
        async def retry():
            return await generator.generate_and_upload_document(
                "token", document_id=first["document_id"], dbsheet_file_id="own-file", user_id=1,
            )
        active = asyncio.create_task(retry())
        await asyncio.wait_for(started.wait(), 2)
        try:
            second = await retry()
            assert second["delivery_status"] == "uploading" and second["retryable"] is False
        finally:
            finish.set()
        assert (await active)["ok"] is True
        wps.upload_to_drive.assert_awaited_once()
    asyncio.run(run())


def test_corrupt_saved_document_is_not_uploaded():
    first = asyncio.run(generator.generate_and_upload_document("", "报告", CONTENT, "own-file", user_id=1))
    path = next((db.DB_PATH.parent / "generated_documents" / "1").glob("*.docx"))
    path.write_bytes(b"corrupted")
    result = resume(first["document_id"])
    assert result["ok"] is False and result["code"] == "document_unavailable"
    wps.upload_to_drive.assert_not_awaited()


def test_retry_does_not_silently_ignore_new_content():
    first = generate()
    result = generate(document_id=first["document_id"])
    assert result["ok"] is False and result["code"] == "invalid_document_request"
    wps.upload_to_drive.assert_awaited_once()


def test_upload_client_reports_commit_boundary_before_effect(monkeypatch):
    events = []
    monkeypatch.setattr(wps, "_resolve_ai_attachment_folder", AsyncMock(return_value=("drive", "folder")))
    monkeypatch.setattr(wps, "_drive_upload_apply", AsyncMock(return_value={
        "upload_id": "request-1", "store_request": {"url": "https://storage.test/file"},
    }))
    monkeypatch.setattr(wps, "_put_raw", AsyncMock())
    async def commit(*_args):
        assert events == ["prepared", "commit_started"]
        return {"id": "cloud-1"}
    monkeypatch.setattr(wps, "_drive_upload_commit", commit)
    receipt = asyncio.run(REAL_UPLOAD(
        "token", "own-file", "report.docx", b"docx", on_phase=lambda phase, _facts: events.append(phase),
    ))
    assert receipt["uploadId"] == "cloud-1"


def test_destination_change_stops_before_upload_apply(monkeypatch):
    calls = 0
    async def upload(*_args, on_phase, **_kwargs):
        nonlocal calls
        calls += 1
        on_phase("prepared", {"drive_id": "first-drive" if calls == 1 else "different-drive", "parent_id": "folder"})
        if calls == 1:
            raise rejected()
        pytest.fail("saved document must not silently move to another drive")
    wps.upload_to_drive.side_effect = upload
    first = generate()
    result = resume(first["document_id"])
    assert result["code"] == "document_target_changed" and result["uploaded"] is False


def test_failed_commit_receipt_remains_unknown():
    async def upload(*_args, on_phase, **_kwargs):
        on_phase("commit_started", {})
        return {}
    wps.upload_to_drive.side_effect = upload
    first = generate()
    assert first["delivery_status"] == "upload_unknown" and first["retryable"] is False
    assert resume(first["document_id"])["delivery_status"] == "upload_unknown"
    wps.upload_to_drive.assert_awaited_once()


def test_cancellation_before_commit_keeps_retryable_file():
    async def upload(*_args, **_kwargs):
        raise asyncio.CancelledError()
    wps.upload_to_drive.side_effect = upload
    with pytest.raises(asyncio.CancelledError):
        generate()
    with sqlite3.connect(db.DB_PATH) as conn:
        document_id, state = conn.execute("SELECT id,state FROM generated_documents").fetchone()
    assert state == "upload_failed"
    wps.upload_to_drive.side_effect = None
    assert resume(document_id)["ok"] is True


def test_expiry_and_quota_do_not_delete_an_unexpired_pending_file(monkeypatch):
    from core import document_delivery as delivery
    monkeypatch.setattr(delivery, "MAX_DOCUMENTS_PER_USER", 1)
    first = asyncio.run(generator.generate_and_upload_document("", "第一份", CONTENT, "own-file", user_id=1))
    second = asyncio.run(generator.generate_and_upload_document("", "第二份", CONTENT, "own-file", user_id=1))
    assert second["code"] == "document_storage_full"
    assert resume(first["document_id"])["ok"] is True
    third = asyncio.run(generator.generate_and_upload_document("", "第三份", CONTENT, "own-file", user_id=1))
    now = delivery.time.time()
    monkeypatch.setattr(delivery.time, "time", lambda: now + delivery.RETENTION_SECONDS + 1)
    assert resume(third["document_id"])["code"] == "document_unavailable"


def test_stale_precommit_claim_can_resume_but_unknown_commit_cannot():
    first = asyncio.run(generator.generate_and_upload_document("", "报告", CONTENT, "own-file", user_id=1))
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("UPDATE generated_documents SET state='uploading', owner='dead', lease_until=0 WHERE id=?", (first["document_id"],))
    assert resume(first["document_id"])["ok"] is True
    second = asyncio.run(generator.generate_and_upload_document("", "另一份", CONTENT, "own-file", user_id=1))
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("UPDATE generated_documents SET state='upload_unknown', owner='dead', lease_until=0 WHERE id=?", (second["document_id"],))
    assert resume(second["document_id"])["delivery_status"] == "upload_unknown"
    wps.upload_to_drive.assert_awaited_once()


def test_lost_upload_claim_cannot_start_a_commit(monkeypatch):
    first = asyncio.run(generator.generate_and_upload_document("", "报告", CONTENT, "own-file", user_id=1))
    async def upload(*_args, on_phase, **_kwargs):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("UPDATE generated_documents SET owner='new-worker' WHERE id=?", (first["document_id"],))
        on_phase("commit_started", {})
        pytest.fail("revoked owner cannot submit")
    wps.upload_to_drive.side_effect = upload
    result = resume(first["document_id"])
    assert result["ok"] is False and result["retryable"] is False
    assert result["code"] == "document_busy"


def test_cloud_success_followed_by_local_storage_failure_does_not_replay(monkeypatch):
    from core import document_delivery as delivery
    original = delivery._settle
    def settle(uid, row, owner, state, cloud_file_id=""):
        if state == "uploaded":
            raise sqlite3.OperationalError("simulated disk failure")
        return original(uid, row, owner, state, cloud_file_id)
    monkeypatch.setattr(delivery, "_settle", settle)
    async def upload(*_args, on_phase, **_kwargs):
        on_phase("commit_started", {})
        return {"id": "cloud-accepted"}
    wps.upload_to_drive.side_effect = upload
    first = generate()
    assert first["uploaded"] is True and first["cloud_file_id"] == "cloud-accepted"
    assert first["code"] == "document_tracking_failed" and first["retryable"] is False
    assert resume(first["document_id"])["delivery_status"] == "upload_unknown"
    wps.upload_to_drive.assert_awaited_once()


def _conversation_with_input(uid=1):
    db.init_db()
    conv_id = db.create_conversation(uid, "文档处理")
    db.add_chat(uid, "user", "请生成报告", conv_id=conv_id)
    return conv_id


def test_followup_references_are_scoped_and_history_clear_stops_recall():
    from core.document_delivery import recovery_context
    conv_id = _conversation_with_input()
    first = generate(conv_id=conv_id)
    assert first["document_id"] in recovery_context(1, conv_id)
    assert not recovery_context(2, conv_id)
    assert not recovery_context(1, conv_id + 1)
    db.clear_chat_history(1, conv_id)
    assert not recovery_context(1, conv_id)
    # Clearing history must not erase a receipt needed to avoid duplicate writes.
    assert resume(first["document_id"])["cloud_file_id"] == "cloud-1"
    wps.upload_to_drive.assert_awaited_once()


def test_chat_followup_reuses_saved_document_without_rewriting_content(monkeypatch):
    from agent import assistant as agent
    conv_id = _conversation_with_input()
    first = asyncio.run(generator.generate_and_upload_document(
        "", "报告", CONTENT, "own-file", user_id=1, conv_id=conv_id,
    ))
    monkeypatch.setattr(generator, "_apply_document_template", lambda *_a: pytest.fail("must reuse DOCX"))
    calls = []
    async def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            assert first["document_id"] in kwargs["messages"][0]["content"]
            tools = [SimpleNamespace(id="resume-document", function=SimpleNamespace(
                name="generate_document", arguments=json.dumps({"document_id": first["document_id"]}),
            ))]
            content = None
        else:
            tools, content = None, "原文档已补传。"
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=content, tool_calls=tools, reasoning_content=None,
        ))])
    assistant = agent.Assistant.__new__(agent.Assistant)
    assistant.provider, assistant.model = "mock", "mock-model"
    assistant.supports_tools, assistant.supports_vision = True, False
    assistant.smart_tool_routing, assistant.reasoning_mode = False, "off"
    assistant.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assistant._auto_learn = AsyncMock()
    result = asyncio.run(assistant.chat(
        [{"role": "user", "content": "继续上传刚才的报告"}], "current-token",
        default_file={"file_id": "own-file", "file_name": "业务表"}, uid=1, conv_id=conv_id,
    ))
    assert result == "原文档已补传。"
    assert len(calls) == 2
    wps.upload_to_drive.assert_awaited_once()
    assert assistant.last_tool_receipts[0]["result"]["cloud_file_id"] == "cloud-1"
    assert not any(key.startswith("_document_") for key in assistant.last_tool_receipts[0]["args"])


def test_mcp_preserves_failed_delivery_reference_and_resumes_with_own_identity(monkeypatch):
    from core import mcp_server as service
    monkeypatch.setattr(service, "_wps_access_token", AsyncMock(return_value="user-token"))
    monkeypatch.setattr(db, "get_default_wps_file", lambda _uid: {"file_id": "own-file"})
    monkeypatch.setattr(db, "list_wps_files", lambda _uid: [{"file_id": "own-file"}])
    audit = Mock()
    monkeypatch.setattr(db, "add_mcp_audit_log", audit)
    wps.upload_to_drive.side_effect = rejected()
    async def run():
        context = service._identity_var.set({"user_id": 1, "id": 12, "scopes": ["all"]})
        try:
            first = await service.execute_tool("generate_document", {
                "title": "报告", "content": CONTENT, "_document_user_id": 999,
            })
            assert first["ok"] is False and first["document_id"]
            assert first["generated"] and first["retained"] and first["retryable"]
            monkeypatch.setattr(generator, "_apply_document_template", lambda *_a: pytest.fail("must reuse DOCX"))
            wps.upload_to_drive.side_effect = None
            second = await service.execute_tool("generate_document", {"document_id": first["document_id"]})
            assert second["ok"] is True and second["cloud_file_id"] == "cloud-1"
            assert wps.upload_to_drive.await_args.args[1] == "own-file"
        finally:
            service._identity_var.reset(context)
    asyncio.run(run())
    assert audit.call_count == 2
    assert audit.call_args_list[0].args[4] is False
    assert audit.call_args_list[1].args[4] is True


def test_read_only_mcp_token_cannot_resume_a_document(monkeypatch):
    from core import mcp_server as service
    from mcp.server.fastmcp.exceptions import ToolError
    first = generate()
    token = AsyncMock()
    monkeypatch.setattr(service, "_wps_access_token", token)
    monkeypatch.setattr(db, "add_mcp_audit_log", Mock())
    context = service._identity_var.set({"user_id": 1, "id": 12, "scopes": ["read"]})
    try:
        with pytest.raises(ToolError):
            asyncio.run(service.execute_tool("generate_document", {"document_id": first["document_id"]}))
    finally:
        service._identity_var.reset(context)
    token.assert_not_awaited()
    wps.upload_to_drive.assert_awaited_once()


def test_web_chat_can_continue_failed_document_upload_across_requests(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api import app_new_routes as routes
    from agent import assistant as agent
    db.init_db()
    monkeypatch.setattr(routes, "_current_user", lambda _r: (1, {"username": "tester", "role": "staff"}))
    monkeypatch.setattr(routes, "ensure_access_token", AsyncMock(return_value="current-user-token"))
    monkeypatch.setattr(db, "get_llm_key", lambda _uid: {"api_key": "mock", "provider": "mock", "advanced": {}})
    monkeypatch.setattr(db, "get_default_wps_file", lambda _uid: {"file_id": "own-file", "file_name": "业务表"})
    monkeypatch.setattr(db, "list_wps_files", lambda _uid: [{"file_id": "own-file", "file_name": "业务表"}])
    assistants = []
    def make_assistant(**_kwargs):
        instance = agent.Assistant.__new__(agent.Assistant)
        instance.provider, instance.model = "mock", "mock-model"
        instance.supports_tools, instance.supports_vision = True, False
        instance.smart_tool_routing, instance.reasoning_mode = False, "off"
        instance._auto_learn = AsyncMock()
        turn = len(assistants)
        calls = []
        async def create(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                if turn == 0:
                    arguments = {"title": "报告", "content": CONTENT}
                else:
                    match = re.search(r'"document_id": "([0-9a-f]{32})"', kwargs["messages"][0]["content"])
                    assert match, "follow-up must recover the actual saved reference"
                    arguments = {"document_id": match[1]}
                tools = [SimpleNamespace(id="document", function=SimpleNamespace(
                    name="generate_document", arguments=json.dumps(arguments, ensure_ascii=False),
                ))]
                content = None
            else:
                tools, content = None, "成品已保留，待补传。" if turn == 0 else "原文档已补传到WPS。"
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=content, tool_calls=tools, reasoning_content=None,
            ))])
        instance.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        assistants.append(instance)
        return instance
    monkeypatch.setattr(routes, "Assistant", make_assistant)
    app = FastAPI()
    app.include_router(routes.app_new_router)
    wps.upload_to_drive.side_effect = rejected()
    with TestClient(app) as client:
        first = client.post("/api/app-new/chat", json={"text": "生成报告", "conversation_id": 0})
        assert "成品已保留" in first.text
        conv_id = db.get_last_active_conv_id(1)
        original = wps.upload_to_drive.await_args.args[3]
        wps.upload_to_drive.side_effect = None
        monkeypatch.setattr(generator, "_apply_document_template", lambda *_a: pytest.fail("must not regenerate"))
        second = client.post("/api/app-new/chat", json={"text": "继续上传刚才的报告", "conversation_id": conv_id})
        assert "原文档已补传" in second.text
    assert wps.upload_to_drive.await_count == 2
    assert wps.upload_to_drive.await_args.args[3] == original
    assert len(assistants) == 2
