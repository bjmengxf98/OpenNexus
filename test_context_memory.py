from auth import db
from core.context_memory import (
    build_user_context,
    recall_chat_history,
    sanitize_memory_content,
)


def _temporary_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "context-test.db")
    db.init_db()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO users (id, username, email, password_hash) VALUES (1,'u1','u1@test','x')"
        )
        conn.execute(
            "INSERT INTO users (id, username, email, password_hash) VALUES (2,'u2','u2@test','x')"
        )


def test_structured_memory_isolated_by_user_topic_and_file(monkeypatch, tmp_path):
    _temporary_db(monkeypatch, tmp_path)
    conv_1 = db.create_conversation(1, "project-a")
    conv_2 = db.create_conversation(2, "project-b")

    db.save_memory_item(1, "prefer concise answers", scope_type="global")
    db.save_memory_item(1, "topic belongs to project a", scope_type="conversation", scope_id=str(conv_1))
    db.save_memory_item(1, "inspection records require an owner", scope_type="file", scope_id="file-a")
    db.save_memory_item(1, "finance records require an invoice", scope_type="file", scope_id="file-b")
    db.save_memory_item(2, "user two private memory", scope_type="conversation", scope_id=str(conv_2))

    context = build_user_context(
        1, conv_1, "continue inspection records",
        default_file={"file_id": "file-a", "file_name": "inspection"},
        all_files=[{"file_id": "file-a", "file_name": "inspection"}],
    )
    assert "prefer concise answers" in context
    assert "topic belongs to project a" in context
    assert "inspection records require an owner" in context
    assert "finance records require an invoice" not in context
    assert "user two private memory" not in context


def test_deleting_topic_also_deletes_only_its_derived_memory(monkeypatch, tmp_path):
    _temporary_db(monkeypatch, tmp_path)
    first = db.create_conversation(1, "first")
    second = db.create_conversation(1, "second")
    db.save_memory_item(1, "first topic only", scope_type="conversation", scope_id=str(first))
    db.save_memory_item(1, "second topic only", scope_type="conversation", scope_id=str(second))

    db.delete_conversation(first, 1)

    assert not db.list_memory_items(1, scope_type="conversation", scope_ids=[str(first)])
    remaining = db.list_memory_items(1, scope_type="conversation", scope_ids=[str(second)])
    assert [item["content"] for item in remaining] == ["second topic only"]


def test_history_recall_uses_only_same_users_original_messages(monkeypatch, tmp_path):
    _temporary_db(monkeypatch, tmp_path)
    conv_1 = db.create_conversation(1, "airport project")
    conv_2 = db.create_conversation(2, "another user")
    db.add_chat(1, "user", "airport project quality inspection report every Friday", conv_id=conv_1)
    db.add_chat(1, "assistant", "AI guessed that the airport project has a critical risk", conv_id=conv_1)
    db.add_chat(2, "user", "airport project private budget is nine million", conv_id=conv_2)

    rows = recall_chat_history(1, "airport project quality inspection report")
    recalled = "\n".join(row["content"] for row in rows)
    assert "every Friday" in recalled
    assert "AI guessed" not in recalled
    assert "private budget" not in recalled


def test_attachment_payload_and_runtime_wps_config_do_not_enter_memory(monkeypatch, tmp_path):
    _temporary_db(monkeypatch, tmp_path)
    conv_id = db.create_conversation(1, "attachment")
    request_text = "\u8bf7\u5904\u7406\u8fd9\u4efd\u65b9\u6848"
    db.add_chat(
        1, "user",
        request_text + "\n\u3010\u6587\u4ef6\uff1a\u65b9\u6848.docx\u3011\nprivate text C:/temp/private.docx",
        conv_id=conv_id,
    )
    rows = recall_chat_history(1, "\u4e4b\u524d\u5904\u7406\u65b9\u6848\u7684\u8981\u6c42")
    assert rows
    assert rows[0]["content"] == request_text

    safe, rejected = sanitize_memory_content(
        "prefer concise answers\ndefault table file_id=secret-file\nmeeting every Friday"
    )
    assert "prefer concise answers" in safe
    assert "meeting every Friday" in safe
    assert "secret-file" not in safe
    assert rejected


def test_context_builder_falls_back_to_legacy_memory(monkeypatch, tmp_path):
    _temporary_db(monkeypatch, tmp_path)

    def fail(*_args, **_kwargs):
        raise RuntimeError("simulated retrieval failure")

    monkeypatch.setattr(db, "list_memory_items", fail)
    assert build_user_context(1, 0, "test", legacy_memory="legacy remains available") == "legacy remains available"
