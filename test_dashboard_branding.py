"""Business-scoped dashboard wording without rewriting real source data."""

from datetime import date
from pathlib import Path

from core import dashboard_service


ROOT = Path(__file__).parent


def test_dashboard_page_and_pwa_shortcut_use_business_name():
    page = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "<title>业务智能驾驶舱 - OpenNexus 业务智能助手</title>" in page
    assert "<h1>业务智能驾驶舱</h1>" in page
    assert "业务概览仪表盘" in page
    assert "部门智能驾驶舱" not in page
    assert '"name": "业务智能驾驶舱", "short_name": "驾驶舱"' in app


def test_new_reports_use_business_wording_and_keep_real_file_name():
    target = date(2026, 8, 2)
    rows = [{"id": "1", "fields": {
        "填报日期": "2026-08-02", "填报人": "甲",
        "进展内容": "处理文件 12 次", "关联任务": "部门协调",
    }}]
    daily = dashboard_service._daily_payload(rows, target, {}, {})
    assert daily["title"] == "业务数据 · 2026/08/02 工作情况分析"
    assert "当日业务事项呈现" in daily["report"]["overview"]
    assert any("业务模板" in item["body"] for item in daily["report"]["recommendations"])
    assert daily["sections"][0]["items"][0]["subtitle"] == "部门协调"

    tasks = dashboard_service._work_payload([], "tasks", target, {}, {})
    projects = dashboard_service._work_payload([], "projects", target, {}, {})
    overview = dashboard_service._overview_payload(
        daily, tasks, projects, target, {"file_name": "部门事务"}
    )
    assert overview["title"] == "业务智能驾驶舱"
    assert overview["source"]["file_name"] == "部门事务"


def test_old_rule_snapshot_is_displayed_with_new_labels_without_rewriting_data():
    old_recommendation = (
        "把高工作量、可复用的处理方法整理为部门模板或标准操作流程，供其他成员复用。"
    )
    stored = {
        "view": "overview", "title": "部门整体驾驶舱",
        "source": {"file_name": "部门事务"},
        "report": {
            "source": "rules", "overview": "部门工作呈现“多项业务”并行推进的特点。",
            "recommendations": [{"title": "沉淀高价值工作方法", "body": old_recommendation}],
        },
        "sections": [{"items": [{"name": "部门协调"}]}],
    }
    result = dashboard_service._normalize_cached_dashboard_labels(stored)

    assert result["title"] == "业务智能驾驶舱"
    assert result["report"]["overview"] == "当日业务事项呈现“多项业务”并行推进的特点。"
    assert "业务模板" in result["report"]["recommendations"][0]["body"]
    assert result["source"]["file_name"] == "部门事务"
    assert result["sections"][0]["items"][0]["name"] == "部门协调"
    assert stored["title"] == "部门整体驾驶舱"
    assert stored["report"]["recommendations"][0]["body"] == old_recommendation


def test_old_ai_snapshot_keeps_historical_analysis_text():
    stored = {
        "view": "overview", "title": "部门整体驾驶舱",
        "report": {"source": "ai", "overview": "部门工作实际是本次分析主题。"},
    }
    result = dashboard_service._normalize_cached_dashboard_labels(stored)
    assert result["title"] == "业务智能驾驶舱"
    assert result["report"]["overview"] == stored["report"]["overview"]
