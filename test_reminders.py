import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import auth.db as db
from core.reminder_text import format_reminder_push_text, normalize_reminder_schedule
from agent.assistant import (
    Assistant,
    _append_tool_result_user_bridge,
    _coalesce_system_messages,
    _detect_event_reminder_scene,
    _detect_personal_reminder_intent,
    _needs_tool_result_user_bridge,
)


class ReminderPushTextTests(unittest.TestCase):
    def test_model_date_is_corrected_from_explicit_tomorrow(self):
        remind_at, event_at = normalize_reminder_schedule(
            "明天提醒我上午9点开会",
            "2026-08-01 08:30",
            "2026-08-01 09:00",
            now=datetime(2026, 8, 1, 20, 22),
        )
        self.assertEqual(remind_at, "2026-08-02 08:30")
        self.assertEqual(event_at, "2026-08-02 09:00")

    def test_date_correction_preserves_travel_lead_time(self):
        remind_at, event_at = normalize_reminder_schedule(
            "提醒我明天下午2点去民航局开会",
            "2026-08-01 12:30",
            "2026-08-01 14:00",
            now=datetime(2026, 8, 1, 20, 22),
        )
        self.assertEqual(remind_at, "2026-08-02 12:30")
        self.assertEqual(event_at, "2026-08-02 14:00")

    def test_subject_word_tomorrow_does_not_change_schedule(self):
        remind_at, event_at = normalize_reminder_schedule(
            "上午9点提醒我讨论明天计划",
            "2026-08-02 08:30",
            "2026-08-02 09:00",
            now=datetime(2026, 8, 1, 20, 22),
        )
        self.assertEqual(remind_at, "2026-08-02 08:30")
        self.assertEqual(event_at, "2026-08-02 09:00")

    def test_tomorrow_becomes_today_when_reminder_is_delivered(self):
        text = format_reminder_push_text(
            "明天上午9点开会，请提前做好准备",
            "2026-08-02 09:00",
            "2026-08-02 08:30",
            now=datetime(2026, 8, 2, 8, 30),
        )
        self.assertIn("【今日提醒】今天上午9点开会", text)
        self.assertIn("（今天（周日）09:00）", text)
        self.assertNotIn("明天上午", text)

    def test_weekday_phrase_is_recalculated_at_delivery_time(self):
        text = format_reminder_push_text(
            "下周三下午2点去民航局开会",
            "2026-08-05 14:00",
            "2026-08-05 12:30",
            now=datetime(2026, 8, 4, 12, 30),
        )
        self.assertIn("【明日提醒】明天下午2点去民航局开会", text)

    def test_relative_word_inside_subject_is_not_rewritten(self):
        text = format_reminder_push_text(
            "讨论明天计划",
            "2026-08-02 09:00",
            "2026-08-02 08:30",
            now=datetime(2026, 8, 2, 8, 30),
        )
        self.assertIn("【今日提醒】讨论明天计划", text)


class ReminderIntentTests(unittest.TestCase):
    def test_system_messages_are_coalesced_at_the_beginning(self):
        result = _coalesce_system_messages([
            {"role": "system", "content": "基础规则"},
            {"role": "user", "content": "问题"},
            {"role": "system", "content": "实时规则"},
            {"role": "assistant", "content": "回答"},
        ])
        self.assertEqual([item["role"] for item in result], ["system", "user", "assistant"])
        self.assertIn("基础规则", result[0]["content"])
        self.assertIn("实时规则", result[0]["content"])

    def test_qwen_tool_result_bridge_only_matches_the_exact_gateway_error(self):
        messages = [
            {"role": "system", "content": "规则"},
            {"role": "user", "content": "查询部门情况"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "{}"},
        ]
        exact = RuntimeError("no user query found in messages")
        unrelated = RuntimeError("upstream internal server error")

        self.assertTrue(_needs_tool_result_user_bridge(exact, messages))
        self.assertFalse(_needs_tool_result_user_bridge(unrelated, messages))
        self.assertFalse(_needs_tool_result_user_bridge(exact, messages[:-1]))

        bridged = _append_tool_result_user_bridge(messages)
        self.assertEqual(bridged[-1]["role"], "user")
        self.assertIn("工具返回结果", bridged[-1]["content"])

    def test_screenshot_phrase_is_a_timed_personal_reminder(self):
        self.assertEqual(
            _detect_personal_reminder_intent("九点微信提醒我找小马说差旅平台的事"),
            (True, True),
        )

    def test_future_notice_without_clock_requires_clarification(self):
        self.assertEqual(
            _detect_personal_reminder_intent("我下周三有个会，让他提前微信通知我"),
            (True, False),
        )

    def test_immediate_message_to_someone_is_not_personal_reminder(self):
        self.assertEqual(
            _detect_personal_reminder_intent("发微信告诉张三马上开会"),
            (False, False),
        )

    def test_meeting_time_is_recognized_as_event_time(self):
        text = "下午两点开会，提醒我"
        self.assertEqual(_detect_personal_reminder_intent(text), (True, True))
        self.assertEqual(_detect_event_reminder_scene(text), "event")

    def test_travel_meeting_requires_route_planning(self):
        text = "下午两点去民航局开会，到时候提醒我"
        self.assertEqual(_detect_personal_reminder_intent(text), (True, True))
        self.assertEqual(_detect_event_reminder_scene(text), "travel")


class _FakeCompletions:
    def __init__(self, messages):
        self._messages = iter(messages)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        message = next(self._messages)
        if isinstance(message, Exception):
            raise message
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class ReminderConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_strict_qwen_gateway_retries_only_after_tool_result_error(self):
        tool_call = SimpleNamespace(
            id="list-reminders-qwen",
            function=SimpleNamespace(name="list_reminders", arguments="{}"),
        )
        completions = _FakeCompletions([
            SimpleNamespace(content=None, tool_calls=[tool_call]),
            RuntimeError("500: no user query found in messages"),
            SimpleNamespace(content="当前没有待触发提醒。", tool_calls=None),
        ])
        assistant = Assistant.__new__(Assistant)
        assistant.provider = "custom"
        assistant.model = "qwen-commercial"
        assistant.client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        assistant._auto_learn = AsyncMock(return_value=None)

        with patch("agent.assistant._db_list_reminders", return_value=[]):
            reply = await assistant.chat(
                [{"role": "user", "content": "看看我的提醒"}],
                access_token="",
                uid=2,
            )

        self.assertEqual(reply, "当前没有待触发提醒。")
        self.assertEqual(len(completions.calls), 3)
        self.assertEqual(completions.calls[1]["messages"][-1]["role"], "tool")
        self.assertEqual(completions.calls[2]["messages"][-1]["role"], "user")

    async def test_default_table_state_keeps_single_leading_system_message(self):
        completions = _FakeCompletions([
            SimpleNamespace(content="我是测试模型。", tool_calls=None),
        ])
        assistant = Assistant.__new__(Assistant)
        assistant.provider = "deepseek"
        assistant.model = "deepseek-chat"
        assistant.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        assistant._auto_learn = AsyncMock(return_value=None)

        reply = await assistant.chat(
            [{"role": "user", "content": "你是哪个大模型"}],
            access_token="",
            default_file={"file_id": "file-123", "file_name": "部门事务"},
            all_files=[{"file_id": "file-123", "file_name": "部门事务", "is_default": True}],
            uid=2,
        )

        self.assertEqual(reply, "我是测试模型。")
        sent = completions.calls[0]["messages"]
        self.assertEqual(sent[0]["role"], "system")
        self.assertEqual(sum(item["role"] == "system" for item in sent), 1)
        self.assertIn("本轮最高优先级实时状态", sent[0]["content"])
        self.assertIn("file-123", sent[0]["content"])
        self.assertEqual(sent[-1]["role"], "user")

    async def test_reminder_planning_keeps_single_leading_system_message(self):
        completions = _FakeCompletions([
            SimpleNamespace(content="提醒规划：请告诉我出发地点。", tool_calls=None),
        ])
        assistant = Assistant.__new__(Assistant)
        assistant.provider = "deepseek"
        assistant.model = "deepseek-chat"
        assistant.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        assistant._auto_learn = AsyncMock(return_value=None)

        await assistant.chat(
            [{"role": "user", "content": "明天下午2点去民航局开会，提醒我"}],
            access_token="",
            uid=2,
        )

        sent = completions.calls[0]["messages"]
        self.assertEqual(sent[0]["role"], "system")
        self.assertEqual(sum(item["role"] == "system" for item in sent), 1)
        self.assertIn("本轮最高优先级提醒规则", sent[0]["content"])
        self.assertNotIn("tools", completions.calls[0])

    async def test_ordinary_meeting_is_created_without_second_confirmation(self):
        now = datetime.now(timezone(timedelta(hours=8)))
        wrong_date = now.strftime("%Y-%m-%d")
        expected_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        tool_call = SimpleNamespace(
            id="reminder-call-auto",
            function=SimpleNamespace(
                name="add_reminder",
                arguments=(
                    '{"content":"上午9点开会",'
                    f'"remind_at":"{wrong_date} 08:30",'
                    f'"event_at":"{wrong_date} 09:00"}}'
                ),
            ),
        )
        completions = _FakeCompletions([
            SimpleNamespace(content=None, tool_calls=[tool_call]),
            SimpleNamespace(content="已设置，明天8:30微信提醒你开会。", tool_calls=None),
        ])
        assistant = Assistant.__new__(Assistant)
        assistant.provider = "deepseek"
        assistant.model = "deepseek-chat"
        assistant.client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        assistant._auto_learn = AsyncMock(return_value=None)

        with patch("agent.assistant._db_add_reminder", return_value=43) as add:
            reply = await assistant.chat(
                [{"role": "user", "content": "明天提醒我上午9点开会"}],
                access_token="",
                uid=2,
            )

        self.assertIn("已设置", reply)
        self.assertIn("tools", completions.calls[0])
        # 正常支持 tool 结果的模型仍保持原始协议顺序，
        # 不会被千问的错误兼容分支改写。
        self.assertEqual(completions.calls[1]["messages"][-1]["role"], "tool")
        self.assertEqual(
            completions.calls[0]["tool_choice"]["function"]["name"],
            "add_reminder",
        )
        add.assert_called_once_with(
            2,
            "上午9点开会",
            f"{expected_date} 08:30",
            event_at=f"{expected_date} 09:00",
        )

    async def test_confirmed_plan_is_written_with_event_and_reminder_times(self):
        future_date = (
            datetime.now(timezone(timedelta(hours=8))) + timedelta(days=2)
        ).strftime("%Y-%m-%d")
        tool_call = SimpleNamespace(
            id="reminder-call-1",
            function=SimpleNamespace(
                name="add_reminder",
                arguments=(
                    '{"content":"下午两点开会",'
                    f'"remind_at":"{future_date} 13:30",'
                    f'"event_at":"{future_date} 14:00"}}'
                ),
            ),
        )
        completions = _FakeCompletions([
            SimpleNamespace(content=None, tool_calls=[tool_call]),
            SimpleNamespace(content="已设置，将在13:30通过微信提醒你。", tool_calls=None),
        ])
        assistant = Assistant.__new__(Assistant)
        assistant.provider = "deepseek"
        assistant.model = "deepseek-chat"
        assistant.client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        assistant._auto_learn = AsyncMock(return_value=None)
        history = [
            {"role": "user", "content": f"{future_date}下午两点开会，提醒我"},
            {
                "role": "assistant",
                "content": f"提醒方案：建议{future_date} 13:30提醒，提前30分钟。这样安排可以吗？",
            },
            {"role": "user", "content": "可以"},
        ]

        with patch("agent.assistant._db_add_reminder", return_value=42) as add:
            reply = await assistant.chat(history, access_token="", uid=2)

        self.assertIn("已设置", reply)
        add.assert_called_once_with(
            2,
            "下午两点开会",
            f"{future_date} 13:30",
            event_at=f"{future_date} 14:00",
        )


class ReminderDatabaseTests(unittest.TestCase):
    def setUp(self):
        self._old_db_path = db.DB_PATH
        self._old_get_conn = db.get_conn
        self._tempdir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self._tempdir.name) / "app.db"

        class ClosingConnection(sqlite3.Connection):
            def __exit__(self, exc_type, exc_value, traceback):
                try:
                    return super().__exit__(exc_type, exc_value, traceback)
                finally:
                    self.close()

        def get_test_conn():
            connection = sqlite3.connect(
                db.DB_PATH,
                factory=ClosingConnection,
            )
            connection.row_factory = sqlite3.Row
            return connection

        db.get_conn = get_test_conn
        db.init_reminders_table()
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS users ("
                "id INTEGER PRIMARY KEY, weixin_id TEXT DEFAULT '', "
                "personal_weixin_id TEXT DEFAULT '', display_name TEXT DEFAULT '')"
            )
            conn.execute(
                "INSERT INTO users (id, personal_weixin_id, display_name) VALUES (2, 'wx-test', '测试用户')"
            )

    def tearDown(self):
        db.get_conn = self._old_get_conn
        db.DB_PATH = self._old_db_path
        self._tempdir.cleanup()

    def _rows(self):
        conn = sqlite3.connect(db.DB_PATH)
        try:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(
                "SELECT * FROM reminders ORDER BY id"
            )]
        finally:
            conn.close()

    def test_requested_time_is_stored_exactly_once(self):
        reminder_id = db.add_reminder(
            2,
            "找小马说差旅平台的事",
            "2026-07-30 09:00",
        )
        rows = self._rows()
        self.assertEqual(reminder_id, rows[0]["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["remind_at"], "2026-07-30 09:00")
        self.assertEqual(rows[0]["event_at"], "2026-07-30 09:00")

    def test_event_time_is_stored_separately_from_reminder_time(self):
        db.add_reminder(
            2,
            "下午去民航局开会",
            "2026-08-03 12:30",
            event_at="2026-08-03 14:00",
        )
        row = self._rows()[0]
        self.assertEqual(row["remind_at"], "2026-08-03 12:30")
        self.assertEqual(row["event_at"], "2026-08-03 14:00")

    def test_failed_delivery_is_retained_for_retry(self):
        reminder_id = db.add_reminder(2, "测试提醒", "2026-07-30 09:00")
        db.mark_reminder_failed(
            reminder_id,
            "微信桥接离线",
            now=datetime(2026, 7, 30, 9, 0),
        )
        row = self._rows()[0]
        self.assertEqual(row["retry_count"], 1)
        self.assertEqual(row["next_retry_at"], "2026-07-30 09:01:00")
        self.assertEqual(row["last_error"], "微信桥接离线")

    def test_due_time_uses_explicit_beijing_time(self):
        reminder_id = db.add_reminder(
            2,
            "集团年中工作会",
            "2026-08-04 08:00",
            event_at="2026-08-04 14:00",
        )
        self.assertEqual(
            db.get_due_reminders(now=datetime(2026, 8, 4, 7, 59)),
            [],
        )
        due = db.get_due_reminders(now=datetime(2026, 8, 4, 8, 0))
        self.assertEqual([row["id"] for row in due], [reminder_id])
        self.assertEqual(due[0]["personal_weixin_id"], "wx-test")

    def test_delivery_result_survives_reminder_deletion(self):
        reminder_id = db.add_reminder(
            2, "测试审计", "2026-08-04 08:00", event_at="2026-08-04 09:00"
        )
        db.log_reminder_delivery(
            reminder_id, 2, "2026-08-04 08:00", "2026-08-04 09:00",
            "wechat", "wx-test", "success", "bridge confirmed ok=true",
        )
        db.delete_reminder(reminder_id)
        logs = db.list_reminder_delivery_log(2)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["reminder_id"], reminder_id)
        self.assertEqual(logs[0]["status"], "success")
        self.assertEqual(self._rows(), [])

    def test_cleanup_does_not_delete_undelivered_expired_reminder(self):
        reminder_id = db.add_reminder(2, "仍需送达", "2026-07-30 09:00")
        self.assertEqual(db.cleanup_legacy_reminders(), 0)
        self.assertEqual(self._rows()[0]["id"], reminder_id)


if __name__ == "__main__":
    unittest.main()
