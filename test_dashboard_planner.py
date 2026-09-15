"""大模型声明式驾驶舱设计的校验、缓存和本地计算测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from core import dashboard_planner
from core.dashboard_generic import build_generic_dashboard


class DashboardPlannerTests(unittest.TestCase):
    def setUp(self):
        self.index = {
            "version": 4,
            "sheets": [{"id": "sales", "name": "销售", "total": 2, "complete": True}],
            "schema_sheets": [{
                "id": "sales", "name": "销售", "type": "xlEtDataBaseSheet",
                "fields": [
                    {"name": "客户", "type": "MultiLineText"},
                    {"name": "金额", "type": "Number"},
                    {"name": "状态", "type": "SingleSelect"},
                    {"name": "wps成员ID", "type": "MultiLineText"},
                    {"name": "是否启用", "type": "SingleSelect"},
                ],
            }],
            "skipped_sheets": [],
        }
        self.cache = {"records": [
            {"fields": {"客户": "甲", "金额": 10, "状态": "成交", "wps成员ID": "1592722663", "是否启用": "1", "机密正文": "不应发送"}},
            {"fields": {"客户": "乙", "金额": 20, "状态": "跟进", "wps成员ID": "1592722662", "是否启用": "0"}},
        ]}
        self.load = lambda _sid: self.cache

    def test_model_design_only_accepts_real_fields_and_allowed_operations(self):
        catalog = dashboard_planner._catalog(self.index, self.load)
        raw = {
            "title": "销售驾驶舱",
            "kpis": [
                {"sheet_id": "sales", "operation": "sum", "field": "金额", "label": "销售额"},
                {"sheet_id": "sales", "operation": "python", "field": "金额", "label": "执行代码"},
                {"sheet_id": "sales", "operation": "sum", "field": "不存在", "label": "虚构"},
                {"sheet_id": "sales", "operation": "average", "field": "wps成员ID", "label": "成员ID均值"},
                {"sheet_id": "sales", "operation": "average", "field": "是否启用", "label": "启用均值"},
            ],
            "charts": [{"sheet_id": "sales", "field": "状态", "title": "状态分布", "limit": "错误"}, {"sheet_id": "sales", "field": "wps成员ID", "title": "ID分布"}],
            "tables": [{"sheet_id": "sales", "fields": ["客户", "不存在"]}],
        }
        design = dashboard_planner.validate_design(raw, catalog)
        self.assertEqual(len(design["kpis"]), 1)
        self.assertEqual(design["charts"][0]["limit"], 8)
        self.assertEqual(len(design["charts"]), 1)
        self.assertEqual(design["tables"][0]["fields"], ["客户"])

    def test_validated_design_is_computed_locally(self):
        catalog = dashboard_planner._catalog(self.index, self.load)
        design = dashboard_planner.validate_design({
            "title": "销售经营驾驶舱", "purpose": "观察销售规模和推进状态。",
            "kpis": [{"sheet_id": "sales", "operation": "sum", "field": "金额", "label": "销售额", "unit": "元"}],
            "metrics": [{"sheet_id": "sales", "operation": "distinct_count", "field": "客户", "label": "客户数", "unit": "家"}],
            "charts": [{"sheet_id": "sales", "field": "状态", "title": "推进状态"}],
            "tables": [{"sheet_id": "sales", "title": "销售明细", "fields": ["客户", "金额", "状态"]}],
        }, catalog)
        base = build_generic_dashboard(self.index, self.load, date(2026, 9, 15), {"file_name": "销售"})
        payload = dashboard_planner.apply_design(
            base, self.index, self.load, design,
            {"version": 2, "source": "ai", "status": "ready", "model": "test"},
        )
        self.assertEqual(payload["title"], "销售经营驾驶舱")
        self.assertEqual(payload["kpis"][0]["value"], 30)
        self.assertEqual(payload["metrics"][0]["value"], 2)
        self.assertEqual({item["label"] for item in payload["distributions"][0]["items"]}, {"成交", "跟进"})
        self.assertEqual(payload["sections"][0]["columns"][0]["label"], "客户")
        self.assertEqual(payload["dashboard_design_version"], 2)

    def test_planning_context_contains_schema_but_not_record_values(self):
        catalog = dashboard_planner._catalog(self.index, self.load)
        serialized = json.dumps(
            dashboard_planner._planning_context("销售", self.index, catalog),
            ensure_ascii=False,
        )
        self.assertIn("金额", serialized)
        self.assertNotIn("不应发送", serialized)
        self.assertNotIn("甲", serialized)
        self.assertNotIn("机密正文", serialized)
        self.assertNotIn("wps成员ID", serialized)
        self.assertIn("是否启用", serialized)

    def test_model_is_not_called_without_explicit_consent(self):
        with patch.object(dashboard_planner.db, "get_llm_key", return_value={"api_key": "secret", "model": "test"}), \
             patch.object(dashboard_planner.db, "get_dashboard_data_cache", return_value=None), \
             patch.object(dashboard_planner.db, "save_dashboard_data_cache", side_effect=AssertionError("must not save pending state")), \
             patch.object(dashboard_planner, "_request_design", new=AsyncMock(side_effect=AssertionError("must not call model"))):
            design, metadata = asyncio.run(dashboard_planner.get_or_create_design(
                1, "file", "销售", self.index, self.load,
            ))
        self.assertIsNone(design)
        self.assertEqual(metadata["status"], "pending")

    def test_unconfigured_model_is_cached_as_safe_fallback(self):
        stored = {}
        with patch.object(dashboard_planner.db, "get_llm_key", return_value=None), \
             patch.object(dashboard_planner.db, "get_dashboard_data_cache", return_value=None), \
             patch.object(dashboard_planner.db, "save_dashboard_data_cache", side_effect=lambda _u, _f, _k, value: stored.update(value)), \
             patch.object(dashboard_planner, "_request_design", new=AsyncMock(side_effect=AssertionError("must not call model"))):
            design, metadata = asyncio.run(dashboard_planner.get_or_create_design(
                1, "file", "销售", self.index, self.load,
                allow_model=True,
            ))
        self.assertIsNone(design)
        self.assertEqual(metadata["status"], "unconfigured")
        self.assertEqual(stored["status"], "unconfigured")

    def test_cached_design_avoids_repeated_model_call(self):
        cfg = {"provider": "deepseek", "base_url": "https://example.test", "model": "deepseek-flash", "api_key": "secret"}
        model_key = hashlib.sha256(json.dumps({
            "provider": cfg["provider"], "base_url": cfg["base_url"], "model": cfg["model"],
        }, sort_keys=True).encode("utf-8")).hexdigest()
        catalog = dashboard_planner._catalog(self.index, self.load)
        design = dashboard_planner.validate_design({
            "kpis": [{"sheet_id": "sales", "operation": "count_rows", "field": "", "label": "订单"}],
        }, catalog)
        cached = {
            "version": 2,
            "schema_fingerprint": dashboard_planner._schema_fingerprint(self.index, catalog),
            "model_key": model_key, "status": "ready", "model": cfg["model"], "design": design,
        }
        with patch.object(dashboard_planner.db, "get_llm_key", return_value=cfg), \
             patch.object(dashboard_planner.db, "get_dashboard_data_cache", return_value=cached), \
             patch.object(dashboard_planner.db, "save_dashboard_data_cache", side_effect=AssertionError("must not save")), \
             patch.object(dashboard_planner, "_request_design", new=AsyncMock(side_effect=AssertionError("must not call model"))):
            loaded, metadata = asyncio.run(dashboard_planner.get_or_create_design(
                1, "file", "销售", self.index, self.load,
            ))
        self.assertEqual(loaded["kpis"][0]["label"], "订单")
        self.assertEqual(metadata["source"], "ai_cache")

    def test_link_and_hierarchy_values_are_rendered_as_business_text(self):
        index = {
            "version": 4,
            "sheets": [
                {"id": "projects", "name": "项目", "total": 2, "complete": True},
                {"id": "people", "name": "人员信息", "total": 2, "complete": True},
            ],
            "schema_sheets": [
                {"id": "projects", "name": "项目", "fields": [
                    {"name": "项目负责人", "type": "OneWayLink"},
                    {"name": "员工状态", "type": "Cascade"},
                ]},
                {"id": "people", "name": "人员信息", "fields": [
                    {"name": "姓名", "type": "MultiLineText"},
                    {"name": "wps成员ID", "type": "MultiLineText"},
                ]},
            ],
            "skipped_sheets": [],
        }
        cache = {
            "projects": {"records": [
                {"id": "r1", "fields": {"项目负责人": ["p1"], "员工状态": {"districts": ["在岗", "在职"], "type": "Common"}}},
                {"id": "r2", "fields": {"项目负责人": ["p2"], "员工状态": {"districts": ["离职"], "type": "Common"}}},
            ]},
            "people": {"records": [
                {"id": "p1", "fields": {"姓名": "张三", "wps成员ID": "1590000001"}},
                {"id": "p2", "fields": {"姓名": "李四", "wps成员ID": "1590000002"}},
            ]},
        }
        load = lambda sid: cache[sid]
        catalog = dashboard_planner._catalog(index, load)
        design = dashboard_planner.validate_design({
            "charts": [
                {"sheet_id": "projects", "field": "项目负责人", "title": "负责人分布"},
                {"sheet_id": "projects", "field": "员工状态", "title": "状态分布"},
            ],
            "tables": [{"sheet_id": "projects", "fields": ["项目负责人", "员工状态"]}],
        }, catalog)
        base = build_generic_dashboard(index, load, date(2026, 9, 15), {"file_name": "业务"})
        payload = dashboard_planner.apply_design(
            base, index, load, design,
            {"version": 2, "source": "ai", "status": "ready", "model": "test"},
        )
        labels = [{item["label"] for item in chart["items"]} for chart in payload["distributions"]]
        self.assertEqual(labels[0], {"张三", "李四"})
        self.assertEqual(labels[1], {"在岗 / 在职", "离职"})
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn('"p1"', rendered)
        self.assertNotIn("Common", rendered)
        self.assertNotIn("wps成员ID", rendered)

    def test_overview_indicators_are_deduplicated_and_capped(self):
        catalog = dashboard_planner._catalog(self.index, self.load)
        candidates = [
            {"sheet_id": "sales", "operation": "count_rows", "field": "", "label": "记录数"},
            {"sheet_id": "sales", "operation": "count_nonempty", "field": "客户", "label": "已填客户"},
            {"sheet_id": "sales", "operation": "distinct_count", "field": "客户", "label": "客户数"},
            {"sheet_id": "sales", "operation": "sum", "field": "金额", "label": "销售额"},
            {"sheet_id": "sales", "operation": "average", "field": "金额", "label": "平均金额"},
            {"sheet_id": "sales", "operation": "count_nonempty", "field": "状态", "label": "已填状态"},
        ]
        design = dashboard_planner.validate_design({
            "kpis": candidates,
            "metrics": [candidates[0], *candidates[1:]],
        }, catalog)
        indicators = [*design["kpis"], *design["metrics"]]
        keys = {(item["sheet_id"], item["operation"], item["field"]) for item in indicators}
        self.assertLessEqual(len(design["kpis"]), 4)
        self.assertLessEqual(len(indicators), 6)
        self.assertEqual(len(indicators), len(keys))


if __name__ == "__main__":
    unittest.main()
