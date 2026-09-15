"""按实际工作表字段生成通用驾驶舱，不预设任务或项目模型。"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date
from typing import Any, Callable

from core.dashboard_field_semantics import (
    build_value_context, display_field_value, is_identifier_field, is_numeric_field,
)
from core.dashboard_service import (
    _distribution, _kpi, _plain, _record_fields,
    _pick_sheet,
)


_NUMERIC_NAME = re.compile(r"金额|经费|预算|费用|成本|收入|支出|数量|工时|次数|总计|合计|单价|价格|amount|budget|cost|total|count|price", re.I)
_CATEGORY_NAME = re.compile(r"状态|类别|类型|阶段|级别|优先级|部门|单位|区域|负责人|项目|分类|status|category|type|stage", re.I)
_TEXT_NAME = re.compile(r"名称|标题|描述|内容|备注|说明|链接|网址|name|title|description|notes|url", re.I)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or isinstance(value, (list, dict)):
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value):
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if abs(number) < 1_000_000_000_000 else None


def _display_number(value: float) -> int | float:
    return int(value) if value.is_integer() else round(value, 2)


def _field_summary(
    name: str,
    records: list[dict],
    definitions: dict[str, dict] | None = None,
    link_lookup: dict[str, str] | None = None,
) -> tuple[list[dict], list[dict]]:
    definitions, link_lookup = definitions or {}, link_lookup or {}
    fields: defaultdict[str, list[Any]] = defaultdict(list)
    for record in records:
        for key, value in _record_fields(record).items():
            if value not in (None, "", []):
                fields[str(key)].append(value)

    metrics: list[dict] = []
    distributions: list[tuple[int, dict]] = []
    for field_name, values in fields.items():
        field = definitions.get(field_name, {})
        if is_identifier_field(field_name, field):
            continue
        numeric = [_number(value) for value in values]
        numbers = [value for value in numeric if value is not None]
        numeric_allowed = is_numeric_field(field) if field else True
        if numeric_allowed and len(numbers) >= 2 and len(numbers) >= len(values) * .8:
            total = sum(numbers)
            metric_name = ("合计" if _NUMERIC_NAME.search(field_name) else "平均") + field_name
            metric_value = total if _NUMERIC_NAME.search(field_name) else total / len(numbers)
            metrics.append({"label": f"{name} · {metric_name}", "value": _display_number(metric_value), "unit": ""})
            continue

        if _TEXT_NAME.search(field_name):
            continue
        counts = Counter(display_field_value(value, field, link_lookup)[:48] for value in values)
        counts.pop("", None)
        counts.pop("未解析关联项", None)
        if not counts or len(counts) > 12 or len(values) < 2:
            continue
        if max(len(label) for label in counts) > 36:
            continue
        if len(counts) > max(6, len(values) * .7):
            continue
        priority = 10 if _CATEGORY_NAME.search(field_name) else 0
        if not priority and (len(values) < 8 or len(counts) > len(values) * .5):
            continue
        distributions.append((priority + len(values), _distribution(f"{name} · {field_name}分布", counts)))
    distributions.sort(key=lambda pair: pair[0], reverse=True)
    return metrics[:4], [item for _, item in distributions[:2]]


def _preview_section(
    sheet: dict,
    records: list[dict],
    definitions: dict[str, dict] | None = None,
    link_lookup: dict[str, str] | None = None,
) -> dict:
    definitions, link_lookup = definitions or {}, link_lookup or {}
    name = sheet["name"]
    frequency: Counter = Counter()
    for record in records[:200]:
        frequency.update(_record_fields(record).keys())
    keys = [
        key for key, _ in frequency.most_common()
        if not is_identifier_field(key, definitions.get(key))
    ][:6]
    columns = [{"key": f"f{index}", "label": key} for index, key in enumerate(keys)]
    items = []
    for record in records[:25]:
        values = _record_fields(record)
        items.append({
            f"f{index}": display_field_value(values.get(key), definitions.get(key, {}), link_lookup)[:160]
            for index, key in enumerate(keys)
        })
    fetched = len(records)
    total = sheet.get("total")
    scope = f"已读取 {fetched} 条"
    if not sheet.get("complete"):
        scope += f" / 约 {total} 条，统计仅覆盖已读取记录" if total is not None else "，统计仅覆盖已读取记录"
    return {
        "title": f"{name} · 记录预览（{scope}）",
        "type": "table", "columns": columns, "items": items,
    }


def build_generic_dashboard(
    index: dict,
    load_sheet: Callable[[str], dict | None],
    target: date,
    source: dict,
) -> dict:
    """本地聚合已同步工作表；所有指标只取自所选文件和真实字段。"""
    sheets = index.get("sheets") or []
    record_count = 0
    sheet_counts = Counter()
    metrics: list[dict] = []
    distributions: list[dict] = []
    sections: list[dict] = []
    insights: list[str] = []
    incomplete: list[str] = []
    definitions_by_sheet, link_lookup = build_value_context(index, load_sheet)
    for sheet in sheets:
        cached = load_sheet(str(sheet["id"])) or {}
        records = cached.get("records") or []
        name = sheet["name"]
        record_count += len(records)
        sheet_counts[name] += len(records)
        if not sheet.get("complete"):
            incomplete.append(name)
        definitions = definitions_by_sheet.get(str(sheet["id"]), {})
        field_metrics, field_distributions = _field_summary(name, records, definitions, link_lookup)
        if not sheet.get("complete"):
            for metric in field_metrics:
                metric["label"] = metric["label"].replace(" · ", " · 已读取", 1)
        metrics.extend(field_metrics)
        distributions.extend(field_distributions)
        sections.append(_preview_section(sheet, records, definitions, link_lookup))

    if len(sheets) > 1:
        distributions.insert(0, _distribution("各工作表已读取记录数", sheet_counts))
    skipped = index.get("skipped_sheets") or []
    if skipped:
        insights.append("已跳过不提供记录接口的说明页或非数据工作表：" +
                        "、".join(str(item.get("name") or item.get("id")) for item in skipped))
    if incomplete:
        insights.append("以下工作表只取得部分记录，相关指标不能当作全表总数：" + "、".join(incomplete))
    if not record_count:
        insights.append("所选文件暂未读取到记录；请确认表格内容和当前 WPS 授权。")
    available = ["overview"]
    schema_sheets = index.get("schema_sheets") or []
    for kind in ("daily", "tasks", "projects"):
        if _pick_sheet(schema_sheets, kind):
            available.append(kind)
    kpis = [
        _kpi("可分析工作表", len(sheets), "个", "blue"),
        _kpi("已读取记录", record_count, "条", "cyan"),
    ]
    kpis.extend(_kpi(f"{name} · 已读取", count, "条", "gold") for name, count in sheet_counts.most_common(4))
    overview = (
        f"所选业务文件包含 {len(sheets)} 个可分析数据工作表，已读取 {record_count} 条记录。"
        "指标和分布根据实际字段生成，不预设部门、任务或项目口径。"
    )
    if skipped:
        overview += f"另有 {len(skipped)} 个说明页或非数据工作表不提供记录接口，已安全跳过。"
    if incomplete:
        overview += "部分工作表未完整读取，当前数字仅代表已读取范围，不能作为全表结论。"
    return {
        "analysis_version": 2,
        "view": "overview", "title": "业务智能驾驶舱", "date": target.isoformat(),
        "source": source, "available_views": available,
        "kpis": kpis, "metrics": metrics[:8], "distributions": distributions[:8],
        "insights": insights, "sections": sections,
        "report": {
            "overview": overview, "highlights": [], "people": [], "followups": [],
            "recommendations": [], "source": "rules",
        },
    }
