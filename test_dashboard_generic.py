"""任意 WPS 业务表格驾驶舱的字段推断、分页和旧快照迁移测试。"""

from __future__ import annotations

import asyncio
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from core import dashboard_generic_cache, dashboard_service
from core.dashboard_generic import build_generic_dashboard


class GenericDashboardTests(unittest.TestCase):
    def test_unrelated_business_sheet_uses_its_own_fields(self):
        index = {
            "sheets": [{"id": "sheet-1", "name": "国家课题管理", "total": 3, "complete": True}],
            "schema_sheets": [{"id": "sheet-1", "name": "国家课题管理"}],
        }
        records = {"records": [
            {"fields": {"课题名称": "甲", "经费": 10, "状态": "进行中"}},
            {"fields": {"课题名称": "乙", "经费": 20, "状态": "进行中"}},
            {"fields": {"课题名称": "丙", "经费": 30, "状态": "已完成"}},
        ]}
        payload = build_generic_dashboard(index, lambda _: records, date(2026, 9, 15), {"file_name": "测试"})
        self.assertEqual(payload["available_views"], ["overview"])
        self.assertEqual([item["value"] for item in payload["kpis"][:2]], [1, 3])
        self.assertIn("合计经费", payload["metrics"][0]["label"])
        self.assertEqual(payload["metrics"][0]["value"], 60)
        self.assertIn("状态分布", payload["distributions"][0]["title"])
        self.assertFalse(any("课题名称分布" in item["title"] for item in payload["distributions"]))
        self.assertEqual(payload["sections"][0]["columns"][0]["label"], "课题名称")
        self.assertNotIn("逾期任务", str(payload["kpis"]))

    def test_partial_sheet_never_claims_all_records(self):
        index = {"sheets": [{"id": "1", "name": "客户", "total": 8000, "complete": False}], "schema_sheets": []}
        payload = build_generic_dashboard(
            index, lambda _: {"records": [{"fields": {"客户": "甲"}}]},
            date(2026, 9, 15), {},
        )
        self.assertEqual(payload["kpis"][1]["label"], "已读取记录")
        self.assertEqual(payload["kpis"][1]["value"], 1)
        self.assertIn("不能作为全表结论", payload["report"]["overview"])
        self.assertIn("统计仅覆盖已读取记录", payload["sections"][0]["title"])
        self.assertEqual(payload["report"]["highlights"], [])

    def test_report_count_marks_total_mismatch_incomplete(self):
        async def fake_list(*_args, **_kwargs):
            return {"records": [{"id": "one"}], "has_more": False, "total": 10}
        with patch.object(dashboard_generic_cache, "list_records", side_effect=fake_list):
            result = asyncio.run(dashboard_generic_cache._load_sheet("token", "file", {"id": "sheet"}))
        self.assertFalse(result["complete"])

    def test_sheet_pagination_marks_missing_cursor_incomplete(self):
        async def fake_list(*_args, **kwargs):
            self.assertEqual(kwargs["max_records"], 1000)
            return {"records": [{"id": "a", "fields": {"自定义字段": "值"}}], "has_more": True, "next_page_token": None, "total": 20}

        with patch.object(dashboard_generic_cache, "list_records", side_effect=fake_list):
            result = asyncio.run(dashboard_generic_cache._load_sheet("token", "file", {"id": "sheet", "records_count": 20}))
        self.assertFalse(result["complete"])
        self.assertEqual(result["total"], 20)
        self.assertEqual(len(result["records"]), 1)

    def test_sync_reads_every_sheet_and_keeps_user_scoped_index(self):
        stored = {}

        def save(user_id, file_id, kind, payload):
            stored[(user_id, file_id, kind)] = payload

        async def fake_records(_token, _file, sheet_id, **_kwargs):
            return {"records": [{"id": sheet_id, "fields": {"状态": "正常"}}], "has_more": False, "total": 1}

        schema = {"sheets": [{"id": "a", "name": "客户"}, {"id": "b", "name": "采购"}]}
        with patch.object(dashboard_generic_cache, "_token", new=AsyncMock(return_value="token")), \
             patch.object(dashboard_generic_cache, "get_schema", new=AsyncMock(return_value=schema)), \
             patch.object(dashboard_generic_cache, "list_records", side_effect=fake_records), \
             patch.object(dashboard_generic_cache.db, "save_dashboard_data_cache", side_effect=save):
            result = asyncio.run(dashboard_generic_cache.sync_generic_dashboard_cache(23, "file-1"))
        self.assertTrue(result["ok"])
        self.assertEqual({row["name"] for row in stored[(23, "file-1", "generic_index")]["sheets"]}, {"客户", "采购"})
        self.assertIn((23, "file-1", "generic_sheet:a"), stored)
        self.assertIn((23, "file-1", "generic_sheet:b"), stored)

    def test_sync_skips_flex_paper_instruction_sheet(self):
        stored = {}
        calls = []

        def save(_user, _file, kind, payload):
            stored[kind] = payload

        async def fake_records(_token, _file, sheet_id, **_kwargs):
            calls.append(sheet_id)
            return {"records": [{"id": "one"}], "has_more": False, "total": 1}

        schema = {"sheets": [
            {"id": 6, "name": "质量月报", "type": "xlEtDataBaseSheet"},
            {"id": 8, "name": "月报操作简要说明", "type": "xlEtFlexPaperSheet"},
            {"id": 9, "name": "仪表盘", "type": "xlDbDashBoardSheet"},
        ]}
        with patch.object(dashboard_generic_cache, "_token", new=AsyncMock(return_value="token")), \
             patch.object(dashboard_generic_cache, "get_schema", new=AsyncMock(return_value=schema)), \
             patch.object(dashboard_generic_cache, "list_records", side_effect=fake_records), \
             patch.object(dashboard_generic_cache.db, "save_dashboard_data_cache", side_effect=save):
            result = asyncio.run(dashboard_generic_cache.sync_generic_dashboard_cache(23, "file-1"))
        self.assertEqual(calls, [6])
        self.assertTrue(result["ok"])
        self.assertEqual(result["skipped"][0]["name"], "月报操作简要说明")
        self.assertEqual(result["skipped"][1]["name"], "仪表盘")
        self.assertEqual(stored["generic_index"]["version"], 4)
        payload = build_generic_dashboard(
            stored["generic_index"], lambda _sheet: {"records": [{"fields": {"状态": "正常"}}]},
            date(2026, 9, 15), {},
        )
        self.assertIn("已安全跳过", payload["report"]["overview"])
        self.assertIn("月报操作简要说明", payload["insights"][0])

    def test_old_overview_snapshot_rebuilds_from_generic_data(self):
        index = {"version": 4, "sheets": [{"id": "1", "name": "客户", "total": 1, "complete": True}],
                 "schema_sheets": [{"id": "1", "name": "客户"}]}

        def get_cache(_user, _file, kind):
            if kind == "generic_index":
                return index
            return {"records": [{"fields": {"客户": "甲"}}]}

        with patch.object(dashboard_service.db, "get_dashboard_snapshot", return_value={"view": "overview", "title": "部门整体驾驶舱", "report": {"overview": "旧"}, "generated_at": "2026-09-15 12:00:00"}), \
             patch.object(dashboard_service.db, "list_wps_files", return_value=[{"file_id": "file-1", "file_name": "客户表"}]), \
             patch.object(dashboard_service.db, "get_dashboard_data_cache", side_effect=get_cache), \
             patch.object(dashboard_service.db, "save_dashboard_snapshot") as save:
            payload = asyncio.run(dashboard_service.generate_dashboard(23, "file-1", "overview", "2026-09-15"))
        self.assertEqual(payload["analysis_version"], 2)
        self.assertIn("客户", payload["sections"][0]["title"])
        save.assert_called_once()

    def test_version_two_index_and_snapshot_are_rebuilt(self):
        old_index = {"version": 2, "sheets": [{"id": "old", "name": "旧表", "complete": True}],
                     "schema_sheets": [{"id": "old", "name": "旧表"}]}
        new_index = {"version": 4, "sheets": [{"id": "new", "name": "新表", "complete": True}],
                     "schema_sheets": [{"id": "new", "name": "新表"}], "skipped_sheets": []}
        state = {"index": old_index}

        def get_cache(_user, _file, kind):
            if kind == "generic_index":
                return state["index"]
            return {"records": [{"fields": {"名称": "记录"}}]}

        async def sync(*_args):
            state["index"] = new_index
            return {"ok": True, "updated": {"新表": 1}, "skipped": [], "errors": []}

        snapshot = {"analysis_version": 2, "report": {"overview": "旧快照"},
                    "generated_at": "2026-09-15 12:00:00"}
        with patch.object(dashboard_service.db, "list_wps_files", return_value=[{"file_id": "file-1", "file_name": "测试"}]), \
             patch.object(dashboard_service.db, "get_dashboard_snapshot", return_value=snapshot), \
             patch.object(dashboard_service.db, "get_dashboard_cache_status", return_value=[]), \
             patch.object(dashboard_service.db, "get_dashboard_data_cache", side_effect=get_cache), \
             patch.object(dashboard_service.db, "save_dashboard_snapshot"), \
             patch.object(dashboard_generic_cache, "sync_generic_dashboard_cache", side_effect=sync):
            payload = asyncio.run(dashboard_service.generate_dashboard(23, "file-1", "overview", "2026-09-15"))
        self.assertEqual(payload["sections"][0]["title"].split(" · ")[0], "新表")

    def test_cached_payload_still_checks_current_file_ownership(self):
        with patch.object(dashboard_service.db, "list_wps_files", return_value=[]), \
             patch.object(dashboard_service.db, "get_dashboard_snapshot", side_effect=AssertionError("must not read")):
            with self.assertRaises(dashboard_service.DashboardError):
                asyncio.run(dashboard_service.generate_dashboard(23, "removed-file", "overview"))

    def test_ui_adapts_tabs_and_resets_view_on_file_switch(self):
        from pathlib import Path
        page = (Path(__file__).parent / "static" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("updateViews(data.available_views)", page)
        self.assertIn("selectOverview();load()", page)
        self.assertIn("await loadDates(true)", page)
        self.assertIn("$('dashboard').hidden=!state.hasData", page)
        self.assertIn("正在加载所选业务表格", page)
        self.assertIn("统计范围：所选文件全部数据工作表", page)
        self.assertIn("重新设计与分析", page)
        self.assertIn('id="adaptive-dashboard-layout"', page)
        self.assertIn("repeat(auto-fit,minmax(320px,1fr))", page)
        self.assertIn(".side{display:contents}", page)


if __name__ == "__main__":
    unittest.main()
