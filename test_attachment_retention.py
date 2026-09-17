from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import app_new_routes
from auth import db
from core import upload_queue


def _fresh_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "attachments.db")
    db.init_db()


def test_attachment_survives_clarification_and_is_removed_after_confirmed_upload(
    monkeypatch, tmp_path,
):
    _fresh_db(monkeypatch, tmp_path)
    monkeypatch.setattr(upload_queue, "_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(upload_queue, "_queue", {})
    stored = upload_queue.store(1, "参数.docx", b"document-content")
    retained_path = Path(stored["path"])

    class FakeAssistant:
        calls = 0

        def __init__(self, **_kwargs):
            self.last_run_metrics = {}
            self.last_tool_receipts = []

        async def chat(self, history, *_args, **_kwargs):
            type(self).calls += 1
            assert retained_path.is_file()
            if type(self).calls == 1:
                return "需要您确认目标记录。"
            assert "本对话仍保留" in history[-1]["content"]
            assert str(retained_path) in history[-1]["content"]
            self.last_tool_receipts = [{
                "name": "upload_and_attach",
                "args": {"file_path": str(retained_path)},
                "result": {"ok": True},
            }]
            return "附件已上传。"

    monkeypatch.setattr(
        app_new_routes, "_current_user",
        lambda _request: (1, {"username": "tester", "display_name": "测试用户", "role": "staff"}),
    )
    monkeypatch.setattr(app_new_routes, "Assistant", FakeAssistant)
    monkeypatch.setattr(
        db, "get_llm_key",
        lambda _uid: {"api_key": "test", "provider": "test", "advanced": {}},
    )

    app = FastAPI()
    app.include_router(app_new_routes.app_new_router)
    with TestClient(app) as client:
        first = client.post(
            "/api/app-new/chat",
            json={"text": "上传到任务", "conversation_id": 0, "as_attachment": True},
        )
        assert first.status_code == 200
        conv_id = db.get_last_active_conv_id(1)
        pending = db.list_conversation_uploads(1, conv_id, pending_only=True)
        assert retained_path.is_file()
        assert [item["name"] for item in pending] == ["参数.docx"]

        second = client.post(
            "/api/app-new/chat",
            json={"text": "就按这个处理", "conversation_id": conv_id},
        )
        assert second.status_code == 200

    uploads = db.list_conversation_uploads(1, conv_id)
    assert uploads[0]["status"] == "attached"
    assert not retained_path.exists()


def test_upload_queue_only_deletes_managed_paths(monkeypatch, tmp_path):
    managed_root = tmp_path / "uploads"
    monkeypatch.setattr(upload_queue, "_UPLOAD_ROOT", managed_root)
    monkeypatch.setattr(upload_queue, "_queue", {})
    stored = upload_queue.store(7, "报告.pdf", b"pdf")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")

    deleted = upload_queue.delete_paths([stored["path"], outside])

    assert deleted == [str(Path(stored["path"]).resolve())]
    assert not Path(stored["path"]).exists()
    assert outside.exists()


def test_confirmed_attachment_receipt_is_reconciled_when_reply_fails(
    monkeypatch, tmp_path,
):
    _fresh_db(monkeypatch, tmp_path)
    monkeypatch.setattr(upload_queue, "_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(upload_queue, "_queue", {})
    stored = upload_queue.store(1, "中断后仍已上传.docx", b"content")
    retained_path = Path(stored["path"])

    class FailingAfterToolAssistant:
        def __init__(self, **_kwargs):
            self.last_run_metrics = {}
            self.last_tool_receipts = []

        async def chat(self, *_args, **_kwargs):
            self.last_tool_receipts = [{
                "name": "upload_and_attach",
                "args": {"file_path": str(retained_path)},
                "result": {"ok": True},
            }]
            raise RuntimeError("reply generation failed after tool success")

    monkeypatch.setattr(
        app_new_routes, "_current_user",
        lambda _request: (1, {"username": "tester", "display_name": "测试用户", "role": "staff"}),
    )
    monkeypatch.setattr(app_new_routes, "Assistant", FailingAfterToolAssistant)
    monkeypatch.setattr(
        db, "get_llm_key",
        lambda _uid: {"api_key": "test", "provider": "test", "advanced": {}},
    )

    app = FastAPI()
    app.include_router(app_new_routes.app_new_router)
    with TestClient(app) as client:
        response = client.post(
            "/api/app-new/chat",
            json={"text": "挂附件", "conversation_id": 0, "as_attachment": True},
        )
        assert response.status_code == 200

    conv_id = db.get_last_active_conv_id(1)
    uploads = db.list_conversation_uploads(1, conv_id)
    assert uploads[0]["status"] == "attached"
    assert not retained_path.exists()
