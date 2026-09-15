"""大模型生成声明式驾驶舱方案，系统校验并在本地执行聚合。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from typing import Any, Callable

from openai import AsyncOpenAI

from auth import db
from core.dashboard_field_semantics import (
    build_value_context, display_field_value, is_identifier_field, is_numeric_field,
)
from core.dashboard_service import COLORS, _plain, _record_fields


DESIGN_VERSION = 2
_PLAN_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}
_ALLOWED_OPERATIONS = {"count_rows", "count_nonempty", "distinct_count", "sum", "average"}
_TONES = {"blue", "cyan", "gold", "green", "pink", "red"}


def _sheet_id(sheet: dict) -> str:
    return str(sheet.get("id") or sheet.get("sheet_id") or sheet.get("sheetId") or "")


def _field_name(field: dict) -> str:
    return str(field.get("name") or field.get("title") or field.get("label") or "").strip()


def _field_type(field: dict) -> str:
    return str(field.get("type") or field.get("value_type") or field.get("field_type") or "").strip()


def _catalog(index: dict, load_sheet: Callable[[str], dict | None]) -> dict[str, dict]:
    schema_by_id = {_sheet_id(sheet): sheet for sheet in index.get("schema_sheets") or []}
    result: dict[str, dict] = {}
    for item in index.get("sheets") or []:
        sid = str(item.get("id") or "")
        schema = schema_by_id.get(sid, {})
        ordered: dict[str, str] = {}
        definitions: dict[str, dict] = {}
        for field in schema.get("fields") or schema.get("columns") or []:
            name = _field_name(field)
            if name:
                ordered[name] = _field_type(field)
                definitions[name] = field
        cached = load_sheet(sid) or {}
        records = cached.get("records") or []
        if not ordered:
            # 仅兼容缺少 fields 的旧 schema；正常响应不从记录正文扩展模型输入。
            for record in records[:200]:
                for name in _record_fields(record):
                    ordered.setdefault(str(name), "")
        result[sid] = {
            "id": sid,
            "name": str(item.get("name") or sid),
            "fields": ordered,
            "field_defs": definitions,
            "records": records,
            "complete": bool(item.get("complete")),
            "total": item.get("total"),
        }
    return result


def _schema_fingerprint(index: dict, catalog: dict[str, dict]) -> str:
    value = [{
        "id": sheet["id"], "name": sheet["name"],
        "fields": list(sheet["fields"].items()),
    } for sheet in catalog.values()]
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _planning_context(file_name: str, index: dict, catalog: dict[str, dict]) -> dict:
    # 只发送 schema，不发送记录正文；每表最多 60 个字段，控制首次设计的 Token。
    sheets = []
    for sheet in catalog.values():
        fields = [{"name": name, "type": field_type}
                  for name, field_type in sheet["fields"].items()
                  if not is_identifier_field(name, sheet["field_defs"].get(name))][:60]
        sheets.append({
            "sheet_id": sheet["id"], "sheet_name": sheet["name"],
            "fetched_records": len(sheet["records"]), "complete": sheet["complete"],
            "fields": fields,
        })
    return {
        "file_name": file_name, "sheets": sheets[:24],
        "skipped_non_data_pages": [item.get("name") for item in index.get("skipped_sheets") or []],
    }


def _extract_json(text: str) -> dict | None:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.I)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(cleaned[start:end + 1])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _short(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def validate_design(raw: dict | None, catalog: dict[str, dict]) -> dict | None:
    """只允许已知表、真实字段和白名单聚合，拒绝模型代码/SQL/表达式。"""
    if not isinstance(raw, dict):
        return None

    def indicator(item: Any) -> dict | None:
        if not isinstance(item, dict):
            return None
        sid = str(item.get("sheet_id") or "")
        operation = str(item.get("operation") or "")
        field = str(item.get("field") or "").strip()
        if sid not in catalog or operation not in _ALLOWED_OPERATIONS:
            return None
        if operation != "count_rows":
            if field not in catalog[sid]["fields"]:
                return None
            definition = catalog[sid]["field_defs"].get(field, {})
            if is_identifier_field(field, definition):
                return None
            if operation in {"sum", "average"} and not is_numeric_field(definition):
                return None
        return {
            "sheet_id": sid, "operation": operation, "field": field,
            "label": _short(item.get("label"), 40) or field or catalog[sid]["name"],
            "unit": _short(item.get("unit"), 8),
            "tone": str(item.get("tone") or "blue") if str(item.get("tone") or "blue") in _TONES else "blue",
        }

    kpis = []
    metrics = []
    seen_indicators: set[tuple[str, str, str]] = set()
    for valid in [candidate for item in (raw.get("kpis") or [])[:8]
                  if (candidate := indicator(item))]:
        key = (valid["sheet_id"], valid["operation"], valid["field"])
        if key not in seen_indicators:
            seen_indicators.add(key)
            kpis.append(valid)
        if len(kpis) >= 4:
            break
    for valid in [candidate for item in (raw.get("metrics") or [])[:8]
                  if (candidate := indicator(item))]:
        key = (valid["sheet_id"], valid["operation"], valid["field"])
        if key not in seen_indicators:
            seen_indicators.add(key)
            metrics.append(valid)
        if len(kpis) + len(metrics) >= 6:
            break
    charts = []
    for item in (raw.get("charts") or [])[:6]:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("sheet_id") or "")
        field = str(item.get("field") or "").strip()
        if (sid in catalog and field in catalog[sid]["fields"]
                and not is_identifier_field(field, catalog[sid]["field_defs"].get(field))):
            try:
                limit = int(item.get("limit") or 8)
            except (TypeError, ValueError):
                limit = 8
            charts.append({
                "sheet_id": sid, "field": field,
                "title": _short(item.get("title"), 60) or f"{field}分布",
                "limit": max(2, min(limit, 12)),
            })
    tables = []
    used = set()
    for item in (raw.get("tables") or [])[:12]:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("sheet_id") or "")
        if sid not in catalog or sid in used:
            continue
        fields = [str(name) for name in (item.get("fields") or [])
                  if str(name) in catalog[sid]["fields"]
                  and not is_identifier_field(str(name), catalog[sid]["field_defs"].get(str(name)))][:8]
        if fields:
            used.add(sid)
            tables.append({
                "sheet_id": sid, "fields": fields,
                "title": _short(item.get("title"), 60) or catalog[sid]["name"],
            })
    if not (kpis or metrics or charts or tables):
        return None
    return {
        "title": _short(raw.get("title"), 60) or "业务智能驾驶舱",
        "purpose": _short(raw.get("purpose"), 240),
        "kpis": kpis, "metrics": metrics, "charts": charts, "tables": tables,
    }


async def _request_design(cfg: dict, context: dict, catalog: dict[str, dict]) -> dict | None:
    prompt = """你是企业业务驾驶舱设计师。请只根据给定的 WPS 多维表格结构，设计一份声明式驾驶舱方案。
你只负责选择业务上有意义的字段和聚合，不计算数字，不生成 HTML、SQL、Python 或公式，不虚构不存在的字段。
规则：
1. sheet_id 和 field 必须逐字使用输入中的值；
2. operation 只能是 count_rows、count_nonempty、distinct_count、sum、average；sum/average 只选 schema 明确声明的 Number 或数值公式字段；ID、编号、账号、工号、电话等标识字段不出现在输入中，也不得猜测；
3. 整体情况是管理摘要，不是字段清单：kpis 最多 4 个，kpis 与 metrics 合计最多 6 个；只选有助于判断业务规模、进度、质量、成本、风险或人效的核心指标，相近或重复指标只保留一个；
4. 内部技术字段、配置开关及仅表示存储结构的值不得作为指标；charts 最多 6 个，每个 chart 是有业务意义的分类字段频数分布；tables 最多 8 个，只保留业务上最有用的明细字段；
5. 不把记录数量直接解释为工作成果，不推断输入结构中没有的逾期、风险或完成结论。
只返回 JSON 对象：
{"title":"...","purpose":"...","kpis":[{"sheet_id":"...","operation":"count_rows","field":"","label":"...","unit":"条","tone":"blue"}],"metrics":[],"charts":[{"sheet_id":"...","field":"...","title":"...","limit":8}],"tables":[{"sheet_id":"...","title":"...","fields":["..."]}]}
输入结构：""" + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    client = AsyncOpenAI(
        api_key=cfg["api_key"], base_url=cfg.get("base_url") or None,
        timeout=35, max_retries=0,
    )
    model = str(cfg.get("model") or "")
    if model.lower().endswith("-reasoning"):
        model = model[:-len("-reasoning")]
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 3000,
        "response_format": {"type": "json_object"},
    }
    if cfg.get("provider") == "deepseek" and model in {
        "deepseek-flash", "deepseek-v4-flash", "deepseek-v4-pro"
    }:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    else:
        kwargs["temperature"] = 0.1
    response = await asyncio.wait_for(client.chat.completions.create(**kwargs), timeout=40)
    text = response.choices[0].message.content or ""
    return validate_design(_extract_json(text), catalog)


async def get_or_create_design(
    user_id: int,
    file_id: str,
    file_name: str,
    index: dict,
    load_sheet: Callable[[str], dict | None],
    *,
    force: bool = False,
    allow_model: bool = False,
) -> tuple[dict | None, dict]:
    catalog = _catalog(index, load_sheet)
    fingerprint = _schema_fingerprint(index, catalog)
    cfg = db.get_llm_key(user_id) or {}
    model_key = hashlib.sha256(json.dumps({
        "provider": cfg.get("provider"), "base_url": cfg.get("base_url"), "model": cfg.get("model"),
    }, sort_keys=True).encode("utf-8")).hexdigest()
    lock = _PLAN_LOCKS.setdefault((user_id, file_id), asyncio.Lock())
    async with lock:
        cached = db.get_dashboard_data_cache(user_id, file_id, "dashboard_design") or {}
        if (not force and cached.get("version") == DESIGN_VERSION
                and cached.get("schema_fingerprint") == fingerprint
                and cached.get("model_key") == model_key):
            return cached.get("design"), {
                "version": DESIGN_VERSION, "source": "ai_cache" if cached.get("design") else "fallback",
                "status": cached.get("status") or "unknown", "model": cached.get("model") or "",
            }
        if not allow_model:
            return None, {
                "version": DESIGN_VERSION, "source": "fallback", "status": "pending", "model": "",
            }
        if not cfg.get("api_key") or not cfg.get("model"):
            stored = {
                "version": DESIGN_VERSION, "schema_fingerprint": fingerprint,
                "model_key": model_key, "status": "unconfigured", "design": None, "records": [],
            }
            db.save_dashboard_data_cache(user_id, file_id, "dashboard_design", stored)
            return None, {"version": DESIGN_VERSION, "source": "fallback", "status": "unconfigured", "model": ""}
        try:
            design = await _request_design(cfg, _planning_context(file_name, index, catalog), catalog)
            if not design:
                raise ValueError("模型返回的驾驶舱方案无有效指标或字段")
            status = "ready"
            error = ""
        except Exception as exc:
            print(f"[DASHBOARD DESIGN] fallback: {type(exc).__name__}: {str(exc)[:240]}")
            design = None
            status = "failed"
            error = f"{type(exc).__name__}: {str(exc)[:160]}"
        stored = {
            "version": DESIGN_VERSION, "schema_fingerprint": fingerprint,
            "model_key": model_key, "status": status, "design": design,
            "model": str(cfg.get("model") or ""), "error": error, "records": [],
        }
        db.save_dashboard_data_cache(user_id, file_id, "dashboard_design", stored)
        return design, {
            "version": DESIGN_VERSION, "source": "ai" if design else "fallback",
            "status": status, "model": stored["model"], "error": error,
        }


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or isinstance(value, (list, dict)):
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value):
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _computed_value(item: dict, sheet: dict, link_lookup: dict[str, str]) -> int | float | None:
    records = sheet["records"]
    operation = item["operation"]
    if operation == "count_rows":
        return len(records)
    values = [_record_fields(record).get(item["field"]) for record in records]
    values = [value for value in values if value not in (None, "", [])]
    if operation == "count_nonempty":
        return len(values)
    if operation == "distinct_count":
        definition = sheet["field_defs"].get(item["field"], {})
        visible = [display_field_value(value, definition, link_lookup) for value in values]
        return len({value for value in visible if value})
    numbers = [number for value in values if (number := _number(value)) is not None]
    if not numbers:
        return None
    result = sum(numbers) if operation == "sum" else sum(numbers) / len(numbers)
    return int(result) if result.is_integer() else round(result, 2)


def apply_design(
    payload: dict,
    index: dict,
    load_sheet: Callable[[str], dict | None],
    design: dict | None,
    metadata: dict,
) -> dict:
    result = copy.deepcopy(payload)
    result["dashboard_design_version"] = DESIGN_VERSION
    result["dashboard_design"] = metadata
    insights = result.setdefault("insights", [])
    if not design:
        message = ("当前显示基础字段概览。点击“重新设计与分析”并确认后，大模型将仅根据表结构生成专属驾驶舱方案。"
                   if metadata.get("status") == "pending" else
                   "尚未配置大模型，当前显示基础字段概览。配置模型后可点击“重新设计与分析”完成专属驾驶舱设计。"
                   if metadata.get("status") == "unconfigured"
                   else "大模型驾驶舱设计暂不可用，当前显示基础字段概览；可稍后重新生成智能分析。")
        insights.insert(0, message)
        return result

    catalog = _catalog(index, load_sheet)
    _definitions, link_lookup = build_value_context(index, load_sheet)
    result["title"] = design["title"]
    designed_kpis = []
    for item in design["kpis"]:
        sheet = catalog.get(item["sheet_id"])
        value = _computed_value(item, sheet, link_lookup) if sheet else None
        if value is None:
            continue
        label = item["label"] + ("（已读取）" if not sheet["complete"] else "")
        designed_kpis.append({"label": label, "value": value, "unit": item["unit"], "tone": item["tone"]})
    if designed_kpis:
        result["kpis"] = designed_kpis

    designed_metrics = []
    for item in design["metrics"]:
        sheet = catalog.get(item["sheet_id"])
        value = _computed_value(item, sheet, link_lookup) if sheet else None
        if value is not None:
            label = item["label"] + ("（已读取）" if not sheet["complete"] else "")
            designed_metrics.append({"label": label, "value": value, "unit": item["unit"]})
    result["metrics"] = designed_metrics

    distributions = []
    for item in design["charts"]:
        sheet = catalog.get(item["sheet_id"])
        if not sheet:
            continue
        definition = sheet["field_defs"].get(item["field"], {})
        counts = Counter(
            display_field_value(_record_fields(record).get(item["field"]), definition, link_lookup) or "未解析/未注明"
            for record in sheet["records"]
        )
        values = [{"label": label, "value": value, "color": COLORS[index % len(COLORS)]}
                  for index, (label, value) in enumerate(counts.most_common(item["limit"]))]
        if values:
            distributions.append({"title": item["title"], "items": values})
    if distributions:
        result["distributions"] = distributions

    sections = []
    selected = set()
    for item in design["tables"]:
        sheet = catalog.get(item["sheet_id"])
        if not sheet:
            continue
        selected.add(item["sheet_id"])
        columns = [{"key": f"f{idx}", "label": name} for idx, name in enumerate(item["fields"])]
        rows = []
        for record in sheet["records"][:25]:
            values = _record_fields(record)
            rows.append({
                f"f{idx}": display_field_value(
                    values.get(name), sheet["field_defs"].get(name, {}), link_lookup,
                )[:160]
                for idx, name in enumerate(item["fields"])
            })
        scope = f"已读取 {len(sheet['records'])} 条"
        if not sheet["complete"]:
            scope += "，当前为部分数据"
        sections.append({
            "title": f"{item['title']} · 记录预览（{scope}）",
            "type": "table", "columns": columns, "items": rows,
        })
    # 模型未选择的表仍保留基础预览，确保任何数据表都不会无提示消失。
    if sections:
        base_by_name = {section.get("title", "").split(" · 记录预览", 1)[0]: section
                        for section in result.get("sections") or []}
        sections.extend(base_by_name[sheet["name"]] for sid, sheet in catalog.items()
                        if sid not in selected and sheet["name"] in base_by_name)
        result["sections"] = sections

    insights.insert(0, "本驾驶舱由大模型依据当前表结构设计；所有数值均由系统在本地缓存中校验字段后计算。")
    if design.get("purpose"):
        report = result.get("report") or {}
        report["overview"] = design["purpose"]
        result["report"] = report
    result["dashboard_design"]["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return result
