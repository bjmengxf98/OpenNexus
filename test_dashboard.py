"""部门驾驶舱聚合与快照测试。"""

from __future__ import annotations

import os
import asyncio
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from auth import db
from core.dashboard_cache import cached_daily_dates
from core import dashboard_service
from core.dashboard_service import _daily_payload, _overview_payload, _work_payload


class DashboardAggregationTests(unittest.TestCase):
    def test_daily_only_includes_requested_date(self):
        rows = [
            {"id": "1", "fields": {"填报日期": "2026-08-02", "填报人": "甲", "进展内容": "处理文件 12 次", "关联任务": "任务A"}},
            {"id": "2", "fields": {"填报日期": "2026-08-01", "填报人": "乙", "进展内容": "昨天内容"}},
            {"id": "3", "fields": {"填报日期": "2026-08-02", "填报人": "丙", "进展内容": ""}},
        ]
        payload = _daily_payload(rows, date(2026, 8, 2), {"file_name": "测试"}, {})
        values = {item["label"]: item["value"] for item in payload["kpis"]}
        self.assertEqual(values["当日进展记录"], 2)
        self.assertEqual(values["有内容记录"], 1)
        self.assertEqual(values["待补空白记录"], 1)
        self.assertEqual(payload["sections"][0]["items"][0]["subtitle"], "任务A")
        self.assertIn("2 条进展记录", payload["report"]["overview"])
        self.assertEqual(len(payload["report"]["people"]), 1)
        self.assertTrue(payload["report"]["recommendations"])

    def test_task_status_and_overdue(self):
        rows = [
            {"id": "1", "fields": {"任务名称": "已完成事项", "状态": "已完成", "截止日期": "2026-08-01"}},
            {"id": "2", "fields": {"任务名称": "逾期事项", "状态": "进行中", "截止日期": "2026-08-01"}},
            {"id": "3", "fields": {"任务名称": "风险事项", "状态": "阻塞", "截止日期": "2026-08-09"}},
        ]
        payload = _work_payload(rows, "tasks", date(2026, 8, 2), {"file_name": "测试"}, {})
        values = {item["label"]: item["value"] for item in payload["kpis"]}
        self.assertEqual(values["任务总数"], 3)
        self.assertEqual(values["已完成"], 1)
        self.assertEqual(values["已逾期"], 1)
        self.assertEqual(values["阻塞/暂停"], 1)
        self.assertEqual(payload["sections"][0]["items"][0]["name"], "逾期事项")
        self.assertIn("逾期", payload["report"]["overview"])
        self.assertEqual(len(payload["report"]["followups"]), 2)

    def test_overview_combines_three_views(self):
        daily = _daily_payload([], date(2026, 8, 2), {}, {})
        tasks = _work_payload([], "tasks", date(2026, 8, 2), {}, {})
        projects = _work_payload([], "projects", date(2026, 8, 2), {}, {})
        result = _overview_payload(daily, tasks, projects, date(2026, 8, 2), {"file_name": "测试"})
        self.assertEqual(result["view"], "overview")
        self.assertEqual(len(result["sections"]), 3)

    def test_deepseek_reasoning_alias_uses_supported_api_model(self):
        captured = {}

        class FakeCompletions:
            async def create(self, **kwargs):
                captured.update(kwargs)
                message = type("Message", (), {"content": '{"overview":"模型总结"}'})()
                choice = type("Choice", (), {"message": message})()
                return type("Response", (), {"choices": [choice]})()

        class FakeClient:
            def __init__(self, **_kwargs):
                self.chat = type("Chat", (), {"completions": FakeCompletions()})()

        cfg = {
            "provider": "deepseek",
            "api_key": "test",
            "base_url": "https://example.invalid/v1",
            "model": "deepseek-v4-flash-reasoning",
        }
        payload = {"view": "daily", "date": "2026-08-02", "report": {"overview": "规则总结"}, "sections": []}
        with patch.object(dashboard_service.db, "get_llm_key", return_value=cfg), patch.object(
            dashboard_service, "AsyncOpenAI", FakeClient
        ):
            result = asyncio.run(dashboard_service._enrich_report_with_llm(1, payload))

        self.assertEqual(captured["model"], "deepseek-v4-flash")
        self.assertEqual(captured["reasoning_effort"], "max")
        self.assertEqual(captured["extra_body"]["thinking"]["type"], "enabled")
        self.assertEqual(captured["response_format"], {"type": "json_object"})
        self.assertEqual(captured["max_tokens"], 8000)
        self.assertEqual(result["report"]["overview"], "模型总结")
        self.assertEqual(result["report"]["source"], "ai")

    def test_invalid_reasoning_json_retries_without_thinking(self):
        calls = []

        class FakeCompletions:
            async def create(self, **kwargs):
                calls.append(kwargs)
                text = "输出被截断：{" if len(calls) == 1 else '{"overview":"重试成功"}'
                message = type("Message", (), {"content": text})()
                choice = type("Choice", (), {"message": message, "finish_reason": "length" if len(calls) == 1 else "stop"})()
                return type("Response", (), {"choices": [choice]})()

        class FakeClient:
            def __init__(self, **_kwargs):
                self.chat = type("Chat", (), {"completions": FakeCompletions()})()

        cfg = {
            "provider": "deepseek",
            "api_key": "test",
            "base_url": "https://example.invalid/v1",
            "model": "deepseek-v4-flash-reasoning",
        }
        payload = {"view": "daily", "date": "2026-08-02", "report": {"overview": "规则总结"}, "sections": []}
        with patch.object(dashboard_service.db, "get_llm_key", return_value=cfg), patch.object(
            dashboard_service, "AsyncOpenAI", FakeClient
        ):
            result = asyncio.run(dashboard_service._enrich_report_with_llm(1, payload))

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["extra_body"]["thinking"]["type"], "enabled")
        self.assertEqual(calls[1]["extra_body"]["thinking"]["type"], "disabled")
        self.assertNotIn("reasoning_effort", calls[1])
        self.assertEqual(result["report"]["overview"], "重试成功")
        self.assertEqual(result["report"]["ai_status"], "success")


class DashboardSnapshotTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db_patch = patch.object(db, "DB_PATH", Path(self.path))
        self.db_patch.start()
        db.init_db()
        db.create_user("dash_test", "dash@example.com", "password")
        self.uid = db.get_user_by_email("dash@example.com")["id"]

    def tearDown(self):
        self.db_patch.stop()
        try:
            os.unlink(self.path)
        except PermissionError:
            pass

    def test_snapshot_upsert_and_dates(self):
        db.save_dashboard_snapshot(self.uid, "file-1", "daily", "2026-08-01", {"value": 1})
        db.save_dashboard_snapshot(self.uid, "file-1", "daily", "2026-08-02", {"value": 2})
        db.save_dashboard_snapshot(self.uid, "file-1", "daily", "2026-08-02", {"value": 3})
        row = db.get_dashboard_snapshot(self.uid, "file-1", "daily", "2026-08-02")
        self.assertEqual(row["value"], 3)
        self.assertEqual(
            db.list_dashboard_snapshot_dates(self.uid, "file-1", "daily"),
            ["2026-08-02", "2026-08-01"],
        )

    def test_local_data_cache_and_daily_date_index(self):
        db.save_dashboard_data_cache(self.uid, "file-1", "daily", {
            "sheet_name": "任务每日进展",
            "records": [
                {"id": "a", "fields": {"填报日期": "2026/08/01", "进展内容": "A"}},
                {"id": "b", "fields": {"填报日期": "2026/08/02", "进展内容": "B"}},
                {"id": "c", "fields": {"填报日期": "2026/08/02", "进展内容": "C"}},
            ],
        })
        cached = db.get_dashboard_data_cache(self.uid, "file-1", "daily")
        self.assertEqual(cached["record_count"], 3)
        self.assertEqual(cached["sheet_name"], "任务每日进展")
        self.assertEqual(
            cached_daily_dates(self.uid, "file-1"),
            ["2026-08-02", "2026-08-01"],
        )


if __name__ == "__main__":
    unittest.main()
