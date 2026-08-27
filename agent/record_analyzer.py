"""完整读取多维表格后，在本地进行筛选、统计和紧凑分批。

该模块不改变原有 list_records 行为。它为分析类任务提供一个低 Token 路径：
源数据完整翻页读取一次，统计在本地完成，需要语义阅读的字段再按字符预算分批返回。
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import time
from typing import Any

from agent.wps_client import list_records

ANALYZE_RECORDS_TOOL = {
    "type": "function",
    "function": {
        "name": "analyze_records",
        "description": (
            "完整翻页读取多维表格后，在服务器本地筛选、统计、分组，并按字符预算返回紧凑记录。"
            "分析全年、全部记录、大表汇总、人员/状态统计时优先使用，避免反复把原始 JSON 发给模型。"
            "若需要阅读每条工作内容，设置 include_rows=true 并仅指定必要 fields；"
            "has_more=true 时用 next_row_offset 继续，直到 is_complete=true。只读，不修改表格。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "file_id": {"type": "string", "description": "WPS 多维表格文件 ID"},
                "sheet_id": {"type": "integer", "description": "工作表 ID"},
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "只读取和分析必要字段；大表任务应明确指定",
                },
                "filter": {
                    "type": "object",
                    "description": "直接交给 WPS 的服务端筛选条件",
                    "additionalProperties": True,
                },
                "view_id": {"type": "string", "description": "可选视图 ID"},
                "local_filters": {
                    "type": "array",
                    "description": "服务端读取后执行的本地筛选条件",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "field": {"type": "string"},
                            "operator": {
                                "type": "string",
                                "enum": [
                                    "eq", "ne", "contains", "not_contains", "in",
                                    "gt", "gte", "lt", "lte", "between",
                                    "empty", "not_empty",
                                ],
                            },
                            "value": {},
                            "values": {"type": "array", "items": {}},
                        },
                        "required": ["field", "operator"],
                    },
                },
                "local_filter_mode": {
                    "type": "string",
                    "enum": ["AND", "OR"],
                    "description": "本地筛选条件组合方式，默认 AND",
                },
                "group_by": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "需要分组计数的字段",
                },
                "numeric_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "需要计算合计、平均、最小和最大的数值字段",
                },
                "include_rows": {
                    "type": "boolean",
                    "description": "是否返回紧凑记录。需要逐条理解工作内容时设为 true",
                },
                "row_offset": {
                    "type": "integer",
                    "description": "紧凑记录起始位置；后续批次使用上次 next_row_offset",
                },
                "row_limit": {
                    "type": "integer",
                    "description": "每批最多记录数，默认 200，最大 1000",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "每批紧凑记录字符预算，默认 60000，范围 4000～120000",
                },
            },
            "required": ["file_id", "sheet_id"],
        },
    },
}

_CACHE_TTL_SECONDS = 180
_CACHE_MAX_ENTRIES = 12
_CACHE: OrderedDict[str, tuple[float, list[dict[str, Any]], dict[str, Any]]] = OrderedDict()


@dataclass
class FetchReceipt:
    records: list[dict[str, Any]]
    pages_fetched: int
    source_is_complete: bool
    continuation_error: str = ""

    def metadata(self) -> dict[str, Any]:
        return {
            "source_records": len(self.records),
            "pages_fetched": self.pages_fetched,
            "source_is_complete": self.source_is_complete,
            "continuation_error": self.continuation_error,
        }


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _cache_key(
    access_token: str,
    file_id: str,
    sheet_id: int,
    fields: list[str] | None,
    server_filter: dict[str, Any] | None,
    view_id: str | None,
) -> str:
    payload = {
        "token": hashlib.sha256((access_token or "").encode("utf-8")).hexdigest()[:16],
        "file_id": file_id,
        "sheet_id": int(sheet_id),
        "fields": fields or [],
        "filter": server_filter or {},
        "view_id": view_id or "",
    }
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def _cache_get(key: str) -> FetchReceipt | None:
    now = time.monotonic()
    expired = [
        item_key for item_key, (created, _records, _meta) in _CACHE.items()
        if now - created > _CACHE_TTL_SECONDS
    ]
    for item_key in expired:
        _CACHE.pop(item_key, None)
    item = _CACHE.get(key)
    if not item:
        return None
    created, records, metadata = item
    _CACHE.move_to_end(key)
    return FetchReceipt(
        records=deepcopy(records),
        pages_fetched=int(metadata.get("pages_fetched") or 0),
        source_is_complete=bool(metadata.get("source_is_complete")),
        continuation_error=str(metadata.get("continuation_error") or ""),
    )


def _cache_put(key: str, receipt: FetchReceipt) -> None:
    _CACHE[key] = (time.monotonic(), deepcopy(receipt.records), receipt.metadata())
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX_ENTRIES:
        _CACHE.popitem(last=False)


async def _fetch_all_records(
    access_token: str,
    file_id: str,
    sheet_id: int,
    *,
    fields: list[str] | None,
    server_filter: dict[str, Any] | None,
    view_id: str | None,
) -> FetchReceipt:
    key = _cache_key(access_token, file_id, sheet_id, fields, server_filter, view_id)
    cached = _cache_get(key)
    if cached:
        return cached

    first = await list_records(
        access_token,
        file_id,
        sheet_id,
        page_size=1000,
        fields=fields,
        filter=server_filter,
        view_id=view_id,
    )
    records = list(first.get("records") or [])
    pages = max(1, math.ceil(max(1, len(records)) / 1000))
    has_more = bool(first.get("has_more"))
    token = first.get("next_page_token")
    seen_tokens: set[str] = set()
    continuation_error = ""

    while has_more:
        if not token:
            continuation_error = "数据源表示仍有下一页，但没有返回 next_page_token"
            break
        token = str(token)
        if token in seen_tokens:
            continuation_error = "数据源重复返回同一个 next_page_token"
            break
        seen_tokens.add(token)
        page = await list_records(
            access_token,
            file_id,
            sheet_id,
            page_size=1000,
            page_token=token,
            fields=fields,
            filter=server_filter,
            view_id=view_id,
        )
        records.extend(page.get("records") or [])
        pages += 1
        has_more = bool(page.get("has_more"))
        token = page.get("next_page_token")

    receipt = FetchReceipt(
        records=records,
        pages_fetched=pages,
        source_is_complete=not has_more,
        continuation_error=continuation_error,
    )
    _cache_put(key, receipt)
    return receipt


def _display_value(value: Any, *, max_chars: int = 500) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("name", "text", "value", "title", "display_name"):
            if value.get(key) not in (None, ""):
                return _display_value(value.get(key), max_chars=max_chars)
        text = _stable_json(value)
    elif isinstance(value, list):
        text = "、".join(
            part for item in value
            if (part := _display_value(item, max_chars=max_chars))
        )
    elif isinstance(value, bool):
        text = "是" if value else "否"
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= max_chars else text[:max_chars] + "…"


def _comparable(value: Any) -> Any:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = _display_value(value, max_chars=10_000)
    try:
        return float(text.replace(",", ""))
    except (TypeError, ValueError):
        return text.casefold()


def _matches_condition(fields: dict[str, Any], condition: dict[str, Any]) -> bool:
    field = str(condition.get("field") or "")
    operator = str(condition.get("operator") or "eq").lower()
    values = condition.get("values")
    if not isinstance(values, list):
        values = [condition.get("value")]
    values = [value for value in values if value is not None]
    raw = fields.get(field)
    shown = _display_value(raw, max_chars=20_000)
    comparable = _comparable(raw)
    targets = [_comparable(value) for value in values]

    if operator == "empty":
        return shown == ""
    if operator == "not_empty":
        return shown != ""
    if operator == "contains":
        return any(str(target).casefold() in shown.casefold() for target in values)
    if operator == "not_contains":
        return all(str(target).casefold() not in shown.casefold() for target in values)
    if operator == "in":
        return comparable in targets
    if operator == "between" and len(targets) >= 2:
        try:
            return targets[0] <= comparable <= targets[1]
        except TypeError:
            return False
    if not targets:
        return True
    target = targets[0]
    if operator == "ne":
        return comparable != target
    if operator == "gt":
        try:
            return comparable > target
        except TypeError:
            return False
    if operator == "gte":
        try:
            return comparable >= target
        except TypeError:
            return False
    if operator == "lt":
        try:
            return comparable < target
        except TypeError:
            return False
    if operator == "lte":
        try:
            return comparable <= target
        except TypeError:
            return False
    return comparable == target


def _apply_local_filters(
    records: list[dict[str, Any]],
    conditions: list[dict[str, Any]] | None,
    mode: str,
) -> list[dict[str, Any]]:
    if not conditions:
        return records
    use_or = str(mode or "AND").upper() == "OR"
    result = []
    for record in records:
        fields = record.get("fields") or {}
        matches = [_matches_condition(fields, item) for item in conditions]
        if (any(matches) if use_or else all(matches)):
            result.append(record)
    return result


def _field_names(records: list[dict[str, Any]], requested: list[str] | None) -> list[str]:
    if requested:
        return list(dict.fromkeys(str(item) for item in requested if str(item).strip()))
    names: list[str] = []
    seen: set[str] = set()
    for record in records:
        for name in (record.get("fields") or {}):
            if name not in seen:
                seen.add(name)
                names.append(str(name))
    return names


def _field_statistics(
    records: list[dict[str, Any]],
    names: list[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in names:
        values = [(record.get("fields") or {}).get(name) for record in records]
        shown = [_display_value(value, max_chars=160) for value in values]
        non_empty = [value for value in shown if value]
        counts = Counter(non_empty)
        numeric = []
        for value in values:
            comparable = _comparable(value)
            if isinstance(comparable, (int, float)) and not isinstance(comparable, bool):
                numeric.append(float(comparable))
        item: dict[str, Any] = {
            "non_empty": len(non_empty),
            "empty": len(values) - len(non_empty),
            "unique": len(counts),
            "top_values": [
                {"value": value, "count": count}
                for value, count in counts.most_common(20)
            ],
        }
        if numeric:
            item["numeric"] = {
                "count": len(numeric),
                "sum": round(sum(numeric), 6),
                "average": round(sum(numeric) / len(numeric), 6),
                "min": min(numeric),
                "max": max(numeric),
            }
        result[name] = item
    return result


def _group_statistics(
    records: list[dict[str, Any]],
    group_by: list[str],
    numeric_fields: list[str],
) -> list[dict[str, Any]]:
    if not group_by:
        return []
    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    for record in records:
        fields = record.get("fields") or {}
        key = tuple(_display_value(fields.get(name), max_chars=120) or "（空）" for name in group_by)
        item = groups.setdefault(
            key,
            {
                "group": {name: key[index] for index, name in enumerate(group_by)},
                "count": 0,
                "numeric": {
                    name: {"sum": 0.0, "count": 0}
                    for name in numeric_fields
                },
            },
        )
        item["count"] += 1
        for name in numeric_fields:
            value = _comparable(fields.get(name))
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                item["numeric"][name]["sum"] += float(value)
                item["numeric"][name]["count"] += 1

    rows = []
    for item in groups.values():
        for name, numeric in list(item["numeric"].items()):
            if not numeric["count"]:
                item["numeric"].pop(name)
                continue
            numeric["sum"] = round(numeric["sum"], 6)
            numeric["average"] = round(numeric["sum"] / numeric["count"], 6)
        rows.append(item)
    return sorted(rows, key=lambda item: (-item["count"], _stable_json(item["group"])))[:200]


def _compact_row(record: dict[str, Any], columns: list[str]) -> list[Any]:
    fields = record.get("fields") or {}
    return [
        record.get("id"),
        *[
            _display_value(fields.get(name), max_chars=2000)
            for name in columns
        ],
    ]


def _row_window(
    records: list[dict[str, Any]],
    columns: list[str],
    *,
    offset: int,
    row_limit: int,
    max_chars: int,
) -> tuple[list[list[Any]], int | None]:
    rows: list[list[Any]] = []
    used = 0
    index = max(0, offset)
    stop = min(len(records), index + max(1, min(row_limit, 1000)))
    while index < stop:
        row = _compact_row(records[index], columns)
        size = len(_stable_json(row))
        if rows and used + size > max_chars:
            break
        rows.append(row)
        used += size
        index += 1
    return rows, (index if index < len(records) else None)


async def analyze_records(
    access_token: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    requested_fields = [
        str(item) for item in (arguments.get("fields") or [])
        if str(item).strip()
    ]
    local_filters = arguments.get("local_filters") or []
    group_by = [
        str(item) for item in (arguments.get("group_by") or [])
        if str(item).strip()
    ]
    numeric_fields = [
        str(item) for item in (arguments.get("numeric_fields") or [])
        if str(item).strip()
    ]
    support_fields = [
        str(item.get("field")) for item in local_filters
        if isinstance(item, dict) and str(item.get("field") or "").strip()
    ]
    fields = list(dict.fromkeys(
        requested_fields + support_fields + group_by + numeric_fields
    )) or None
    receipt = await _fetch_all_records(
        access_token,
        str(arguments["file_id"]),
        int(arguments["sheet_id"]),
        fields=fields,
        server_filter=arguments.get("filter"),
        view_id=arguments.get("view_id"),
    )
    filtered = _apply_local_filters(
        receipt.records,
        arguments.get("local_filters"),
        str(arguments.get("local_filter_mode") or "AND"),
    )
    columns = _field_names(filtered, fields)
    include_rows = bool(arguments.get("include_rows", False))
    offset = max(0, int(arguments.get("row_offset") or 0))
    row_limit = max(1, min(int(arguments.get("row_limit") or 200), 1000))
    max_chars = max(4000, min(int(arguments.get("max_chars") or 60_000), 120_000))
    rows: list[list[Any]] = []
    next_offset = None
    if include_rows:
        rows, next_offset = _row_window(
            filtered,
            columns,
            offset=offset,
            row_limit=row_limit,
            max_chars=max_chars,
        )

    result = {
        "ok": True,
        "source_records": len(receipt.records),
        "matched_records": len(filtered),
        "pages_fetched": receipt.pages_fetched,
        "source_is_complete": receipt.source_is_complete,
        "continuation_error": receipt.continuation_error,
        "field_statistics": _field_statistics(filtered, columns),
        "group_statistics": _group_statistics(filtered, group_by, numeric_fields),
        "columns": ["record_id", *columns],
        "rows": rows,
        "row_offset": offset,
        "returned_rows": len(rows),
        "next_row_offset": next_offset,
        "has_more": next_offset is not None,
        "is_complete": receipt.source_is_complete and (not include_rows or next_offset is None),
        "cache_ttl_seconds": _CACHE_TTL_SECONDS,
        "_note": (
            "统计基于全部匹配记录；紧凑记录尚有后续批次，请使用 next_row_offset 继续。"
            if next_offset is not None
            else "统计和所请求的紧凑记录均已完整返回。"
        ),
    }
    if not receipt.source_is_complete:
        result["_note"] = (
            "源数据分页未能完整读取，不能据此声称分析完成。"
            f"原因：{receipt.continuation_error or '未知分页错误'}"
        )
    return result
