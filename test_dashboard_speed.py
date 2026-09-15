"""驾驶舱空快照修复与按需加载回归。"""

from __future__ import annotations

import asyncio
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from core import dashboard_cache, dashboard_service


_ROWS = [
    {"id": str(index), "fields": {"填报日期": "2026/09/14", "填报人": "甲", "进展内容": "已处理"}}
    for index in range(18)
]


class DashboardSpeedTests(unittest.TestCase):
    def _files(self):
        return [{"file_id": "file-1", "file_name": "部门事务"}]

    def test_newer_daily_cache_repairs_old_empty_snapshot_without_wps(self):
        old = {"view": "daily", "report": {"overview": "无记录"},
               "kpis": [{"label": "当日进展记录", "value": 0}],
               "generated_at": "2026-09-15 07:10:00"}

        def data_cache(_user, _file, kind):
            return {"sheet_name": "任务每日进展", "synced_at": "2026-09-15 07:15:21", "records": _ROWS} if kind == "daily" else None

        with patch.object(dashboard_service.db, "list_wps_files", return_value=self._files()), \
             patch.object(dashboard_service.db, "get_dashboard_snapshot", return_value=old), \
             patch.object(dashboard_service.db, "get_dashboard_cache_status", return_value=[
                 {"data_kind": "daily", "synced_at": "2026-09-15 07:15:21"}
             ]), patch.object(dashboard_service.db, "get_dashboard_data_cache", side_effect=data_cache), \
             patch.object(dashboard_service.db, "save_dashboard_snapshot") as save, \
             patch.object(dashboard_cache, "sync_dashboard_cache", new=AsyncMock(side_effect=AssertionError("unnecessary WPS read"))):
            result = asyncio.run(dashboard_service.generate_dashboard(1, "file-1", "daily", "2026-09-14"))
        self.assertEqual(result["kpis"][0]["value"], 18)
        save.assert_called_once()

    def test_positive_snapshot_uses_local_result_until_source_changes(self):
        saved = {"view": "daily", "report": {"overview": "已有记录"},
                 "kpis": [{"label": "当日进展记录", "value": 18}],
                 "generated_at": "2026-09-14 12:00:00"}
        with patch.object(dashboard_service.db, "list_wps_files", return_value=self._files()), \
             patch.object(dashboard_service.db, "get_dashboard_snapshot", return_value=saved), \
             patch.object(dashboard_service.db, "get_dashboard_cache_status", return_value=[
                 {"data_kind": "daily", "synced_at": "2026-09-14 11:00:00"}
             ]), patch.object(dashboard_service.db, "get_dashboard_data_cache", side_effect=AssertionError("unnecessary cache rebuild")):
            result = asyncio.run(dashboard_service.generate_dashboard(1, "file-1", "daily", "2026-09-14"))
        self.assertTrue(result["cached"])
        self.assertEqual(result["kpis"][0]["value"], 18)

    def test_old_zero_snapshot_rechecks_only_selected_date(self):
        old = {"view": "daily", "report": {"overview": "无记录"},
               "kpis": [{"label": "当日进展记录", "value": 0}],
               "generated_at": "2026-09-01 00:00:00"}
        current = {"sheet_name": "任务每日进展", "synced_at": "2026-09-01 00:00:00", "records": []}

        async def sync(*_args, **kwargs):
            self.assertEqual(kwargs["kinds"], {"daily"})
            self.assertEqual(kwargs["target_dates"], [date(2026, 9, 14)])
            current["records"] = _ROWS
            return {"ok": True, "errors": []}

        with patch.object(dashboard_service.db, "list_wps_files", return_value=self._files()), \
             patch.object(dashboard_service.db, "get_dashboard_snapshot", return_value=old), \
             patch.object(dashboard_service.db, "get_dashboard_cache_status", return_value=[]), \
             patch.object(dashboard_service.db, "get_dashboard_data_cache", side_effect=lambda *_args: current if _args[2] == "daily" else None), \
             patch.object(dashboard_service.db, "save_dashboard_snapshot"), \
             patch.object(dashboard_cache, "sync_dashboard_cache", side_effect=sync):
            result = asyncio.run(dashboard_service.generate_dashboard(1, "file-1", "daily", "2026-09-14"))
        self.assertEqual(result["kpis"][0]["value"], 18)

    def test_daily_reuses_complete_generic_sheet_without_wps(self):
        index = {"sheets": [{"id": "sheet-1", "name": "任务每日进展", "complete": True}],
                 "schema_sheets": [{"id": "sheet-1", "name": "任务每日进展"}]}

        def data_cache(_user, _file, kind):
            if kind == "generic_index":
                return index
            if kind == "generic_sheet:sheet-1":
                return {"records": _ROWS, "synced_at": "2026-09-15 07:15:00"}
            return None

        with patch.object(dashboard_service.db, "list_wps_files", return_value=self._files()), \
             patch.object(dashboard_service.db, "get_dashboard_snapshot", return_value=None), \
             patch.object(dashboard_service.db, "get_dashboard_cache_status", return_value=[]), \
             patch.object(dashboard_service.db, "get_dashboard_data_cache", side_effect=data_cache), \
             patch.object(dashboard_service.db, "save_dashboard_snapshot"), \
             patch.object(dashboard_cache, "sync_dashboard_cache", new=AsyncMock(side_effect=AssertionError("unnecessary WPS read"))):
            result = asyncio.run(dashboard_service.generate_dashboard(1, "file-1", "daily", "2026-09-14"))
        self.assertEqual(result["kpis"][0]["value"], 18)

    def test_date_choices_can_come_from_generic_cache(self):
        index = {"schema_sheets": [{"id": "sheet-1", "name": "任务每日进展"}]}

        def data_cache(_user, _file, kind):
            return {"generic_index": index, "generic_sheet:sheet-1": {"records": _ROWS}}.get(kind)

        with patch.object(dashboard_cache.db, "get_dashboard_data_cache", side_effect=data_cache):
            self.assertEqual(dashboard_cache.cached_daily_dates(1, "file-1"), ["2026-09-14"])

    def test_selected_kind_does_not_fetch_other_sheets(self):
        schema = {"sheets": [
            {"id": "daily", "name": "任务每日进展", "records_count": 2},
            {"id": "tasks", "name": "任务", "records_count": 2},
            {"id": "projects", "name": "项目", "records_count": 2},
        ]}
        calls = []

        async def list_records(_token, _file, sheet_id, **_kwargs):
            calls.append(sheet_id)
            return {"records": [], "has_more": False}

        with patch.object(dashboard_cache, "_token", new=AsyncMock(return_value="token")), \
             patch.object(dashboard_cache, "get_schema", new=AsyncMock(return_value=schema)), \
             patch.object(dashboard_cache, "list_records", side_effect=list_records), \
             patch.object(dashboard_cache.db, "get_dashboard_data_cache", return_value={"records": []}), \
             patch.object(dashboard_cache.db, "save_dashboard_data_cache"):
            result = asyncio.run(dashboard_cache.sync_dashboard_cache(
                1, "file-1", target_dates=[date(2026, 9, 14)], kinds={"daily"}
            ))
        self.assertTrue(result["ok"])
        self.assertEqual(calls, ["daily"])


if __name__ == "__main__":
    unittest.main()
