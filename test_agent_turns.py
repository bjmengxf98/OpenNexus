import asyncio
import json
import sqlite3
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import app_new_routes
from api.app_new_routes import _stream_turn_events
from auth import db


def _fresh_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "agent-turns.db")
    db.init_db()


def test_agent_turn_lifecycle_events_and_user_isolation(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    conv_id = db.create_conversation(1, "可恢复任务")
    turn = db.create_agent_turn(1, conv_id, {"text": "生成周报"})

    assert turn["id"].startswith("turn_")
    assert turn["status"] == "queued"
    assert turn["request"] == {"text": "生成周报"}
    assert db.get_agent_turn(turn["id"], 2) is None
    assert db.get_active_agent_turn(1, conv_id)["id"] == turn["id"]

    first = db.add_agent_turn_event(turn["id"], "accepted", {"conversation_id": conv_id})
    second = db.add_agent_turn_event(turn["id"], "tool", {"name": "list_records"})
    assert second > first
    assert db.list_agent_turn_events(turn["id"], 2) == []
    events = db.list_agent_turn_events(turn["id"], 1, after_id=first)
    assert events == [{
        "event_id": second,
        "turn_id": turn["id"],
        "type": "tool",
        "created_at": events[0]["created_at"],
        "name": "list_records",
    }]

    db.update_agent_turn_status(turn["id"], 1, "in_progress")
    cancelled = db.request_agent_turn_cancel(turn["id"], 1)
    assert cancelled["status"] == "cancel_requested"
    assert cancelled["cancel_requested"] is True
    db.update_agent_turn_status(turn["id"], 1, "cancelled")
    assert db.get_active_agent_turn(1, conv_id) is None

    with pytest.raises(ValueError):
        db.update_agent_turn_status(turn["id"], 1, "unknown")


def test_terminal_turn_stream_replays_from_cursor_and_closes(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    conv_id = db.create_conversation(1, "事件续接")
    turn = db.create_agent_turn(1, conv_id, {"text": "继续"})
    accepted_id = db.add_agent_turn_event(
        turn["id"], "accepted", {"conversation_id": conv_id},
    )
    done_id = db.add_agent_turn_event(
        turn["id"], "done", {"conversation_id": conv_id, "reply": "完成"},
    )
    db.update_agent_turn_status(turn["id"], 1, "completed")

    async def collect():
        return [chunk async for chunk in _stream_turn_events(1, turn["id"], accepted_id)]

    chunks = asyncio.run(collect())
    assert len(chunks) == 1
    payload = json.loads(chunks[0].removeprefix("data: ").strip())
    assert payload["type"] == "done"
    assert payload["event_id"] == done_id
    assert payload["reply"] == "完成"


def test_deleting_conversation_cleans_turns_and_events(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    conv_id = db.create_conversation(1, "待删除")
    turn = db.create_agent_turn(1, conv_id, {"text": "测试"})
    db.add_agent_turn_event(turn["id"], "accepted", {"conversation_id": conv_id})

    db.delete_conversation(conv_id, 1)

    assert db.get_agent_turn(turn["id"], 1) is None
    with db.get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM agent_turn_events WHERE turn_id=?", (turn["id"],),
        ).fetchone()[0]
    assert count == 0


def test_existing_chat_endpoint_keeps_sse_contract_and_persists_turn(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)

    class FakeAssistant:
        def __init__(self, **_kwargs):
            pass

        async def chat(self, *_args, **_kwargs):
            return "兼容回复"

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
        response = client.post(
            "/api/app-new/chat", json={"text": "你好", "conversation_id": 0},
        )

    assert response.status_code == 200
    turn_id = response.headers["X-Agent-Turn-ID"]
    payloads = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert [item["type"] for item in payloads] == ["accepted", "done"]
    assert payloads[-1]["reply"] == "兼容回复"
    assert db.get_agent_turn(turn_id, 1)["status"] == "completed"
    conv_id = payloads[0]["conversation_id"]
    assert [row["role"] for row in db.get_chat_history(1, conv_id=conv_id)] == [
        "user", "assistant",
    ]


def test_cancel_endpoint_is_owner_scoped_and_reaches_terminal_state(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    conv_id = db.create_conversation(1, "停止任务")
    turn = db.create_agent_turn(1, conv_id, {"text": "长任务"})
    app = FastAPI()
    app.include_router(app_new_routes.app_new_router)

    monkeypatch.setattr(
        app_new_routes, "_current_user",
        lambda _request: (2, {"username": "other", "role": "staff"}),
    )
    with TestClient(app) as client:
        denied = client.post(f"/api/app-new/turns/{turn['id']}/cancel")
    assert denied.status_code == 404

    monkeypatch.setattr(
        app_new_routes, "_current_user",
        lambda _request: (1, {"username": "owner", "role": "staff"}),
    )
    with TestClient(app) as client:
        stopped = client.post(f"/api/app-new/turns/{turn['id']}/cancel")
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "cancelled"
    assert db.get_agent_turn(turn["id"], 1)["status"] == "cancelled"
    assert db.list_agent_turn_events(turn["id"], 1)[-1]["type"] == "cancelled"


def test_phase_two_database_gets_additive_worker_lease_columns(monkeypatch, tmp_path):
    path = tmp_path / "phase-two.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE agent_turns (
                id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                conversation_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                request_json TEXT NOT NULL DEFAULT '{}', cancel_requested INTEGER DEFAULT 0,
                error TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                completed_at TEXT DEFAULT ''
            )
        """)
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()

    with db.get_conn() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(agent_turns)")}
    assert {"worker_id", "heartbeat_at"}.issubset(columns)


def test_worker_lease_interrupts_only_stale_turns_without_replay(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    stale_conv = db.create_conversation(1, "失联任务")
    fresh_conv = db.create_conversation(1, "正常任务")
    stale = db.create_agent_turn(1, stale_conv, {"text": "可能写入 WPS"})
    fresh = db.create_agent_turn(1, fresh_conv, {"text": "正常执行"})

    assert db.claim_agent_turn(stale["id"], 1, "worker-old") is True
    assert db.claim_agent_turn(fresh["id"], 1, "worker-live") is True
    assert db.heartbeat_agent_turn(fresh["id"], 1, "wrong-worker") is False
    assert db.heartbeat_agent_turn(fresh["id"], 1, "worker-live") is True

    old = (db.beijing_now() - timedelta(minutes=5)).isoformat(timespec="seconds")
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE agent_turns SET heartbeat_at=?, updated_at=? WHERE id=?",
            (old, old, stale["id"]),
        )

    interrupted = db.interrupt_stale_agent_turns(stale_seconds=30)

    assert interrupted == [stale["id"]]
    stale_result = db.get_agent_turn(stale["id"], 1)
    assert stale_result["status"] == "interrupted"
    assert "未自动重放" in stale_result["error"]
    stale_event = db.list_agent_turn_events(stale["id"], 1)[-1]
    assert stale_event["type"] == "error"
    assert stale_event["reason"] == "worker_lost"
    assert db.get_agent_turn(fresh["id"], 1)["status"] == "in_progress"
