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
import time
from typing import Any

from agent.wps_client import list_records
from core.record_evidence import continuation, exhausted, field_state, record_counts, record_scope, valid_page
from core.read_diagnostics import note_read_event, note_source_records

ANALYZE_RECORDS_TOOL = {
    "type": "function",
    "function": {
        "name": "analyze_records",
        "description": (
            "完整翻页读取多维表格后，在服务器本地筛选、统计、分组，并按字符预算返回紧凑记录。"
            "分析全年、全部记录、大表汇总、人员/状态统计时优先使用，避免反复把原始 JSON 发给模型。"
            "若需要阅读每条工作内容，设置 include_rows=true 并仅指定必要 fields；"
            "has_more=true 时用 next_row_offset 继续。not_returned 表示字段未返回，不能当空白；"
            "filter_unknown_records>0 时匹配数尚不确定。源查询、本地筛选和展示窗口分别报告。只读，不修改表格。"
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
                    "description": "需要分组计数的字段；首字段为小计主体，后续字段同时返回主体内分布。例如按主体、状态分组可直接得到每个主体的状态小计，无需自行相加。",
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
_CACHE: OrderedDict[tuple[str, int, str], tuple[float, list[dict[str, Any]], dict[str, Any]]] = OrderedDict()
_CACHE_WRITE_EPOCH = 0


def invalidate_record_cache(file_id: str, sheet_id: int) -> None:
    """Evict this table for all credentials and prevent stale in-flight cache puts."""
    global _CACHE_WRITE_EPOCH
    _CACHE_WRITE_EPOCH += 1
    for key in list(_CACHE):
        if key[:2] == (str(file_id), int(sheet_id)):
            _CACHE.pop(key, None)


@dataclass
class FetchReceipt:
    records: list[dict[str, Any]]
    pages_fetched: int | None
    source_is_complete: bool
    continuation_error: str = ""
    source_scope: dict[str, Any] | None = None
    query_total: int | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "source_records": len(self.records),
            "pages_fetched": self.pages_fetched,
            "source_is_complete": self.source_is_complete,
            "continuation_error": self.continuation_error,
            "source_scope": deepcopy(self.source_scope),
            "query_total": self.query_total,
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
) -> tuple[str, int, str]:
    payload = {
        "token": hashlib.sha256((access_token or "").encode("utf-8")).hexdigest()[:16],
        "file_id": file_id,
        "sheet_id": int(sheet_id),
        "fields": fields or [],
        "filter": server_filter or {},
        "view_id": view_id or "",
    }
    return (str(file_id), int(sheet_id),
            hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest())


def _cache_get(key: tuple[str, int, str]) -> FetchReceipt | None:
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
        source_scope=deepcopy(metadata.get("source_scope")),
        query_total=metadata.get("query_total"),
    )


def _cache_put(key: tuple[str, int, str], receipt: FetchReceipt) -> None:
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
    scope = record_scope(file_id=file_id, sheet_id=sheet_id, fields=fields, filter=server_filter, view_id=view_id)
    cached = _cache_get(key)
    if cached:
        note_read_event("analysis_cache_hit")
        return cached
    note_read_event("analysis_cache_miss")

    fetch_epoch = _CACHE_WRITE_EPOCH

    first = await list_records(
        access_token,
        file_id,
        sheet_id,
        page_size=1000,
        fields=fields,
        filter=server_filter,
        view_id=view_id,
    )
    if not valid_page(first):
        return FetchReceipt([], 1, False, "原始记录读取失败，不作为完整范围。", scope)
    records = list(first["records"])
    pages = int(first.get("pages_fetched") or 1)
    has_more = not exhausted(first)
    token = continuation(first)
    query_total = first.get("total") if first.get("total_is_known") else None
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
        if not valid_page(page):
            continuation_error = "后续记录页读取失败，保留已取部分，不作为完整范围。"
            break
        records.extend(page["records"])
        pages += 1
        has_more = not exhausted(page)
        token = continuation(page)
        if page.get("total_is_known"):
            query_total = page["total"]

    receipt = FetchReceipt(
        records=records,
        pages_fetched=pages,
        source_is_complete=not has_more,
        continuation_error=continuation_error,
        source_scope=scope,
        query_total=query_total,
    )
    if receipt.source_is_complete and fetch_epoch == _CACHE_WRITE_EPOCH:
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


def _matches_condition(fields: dict[str, Any], condition: dict[str, Any]) -> bool | None:
    field = str(condition.get("field") or "")
    if field_state(fields, field) == "not_returned":
        return None
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
        return field_state(fields, field) == "empty"
    if operator == "not_empty":
        return field_state(fields, field) == "value"
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
    *, with_unknown: bool = False,
):
    if not conditions:
        return (records, 0) if with_unknown else records
    use_or = str(mode or "AND").upper() == "OR"
    result = []
    unknown = 0
    for record in records:
        fields = record.get("fields") or {}
        matches = [_matches_condition(fields, item) for item in conditions]
        if use_or:
            decision = True if True in matches else None if None in matches else False
        else:
            decision = False if False in matches else None if None in matches else True
        if decision is True:
            result.append(record)
        elif decision is None:
            unknown += 1
    return (result, unknown) if with_unknown else result


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
        states = Counter(field_state(record.get("fields"), name) for record in records)
        values = [record["fields"][name] for record in records if field_state(record.get("fields"), name) == "value"]
        shown = [_display_value(value, max_chars=160) for value in values]
        non_empty = [value for value in shown if value]
        counts = Counter(non_empty)
        numeric = []
        for value in values:
            comparable = _comparable(value)
            if isinstance(comparable, (int, float)) and not isinstance(comparable, bool):
                numeric.append(float(comparable))
        item: dict[str, Any] = {
            "counted_records": len(records),
            "non_empty": states["value"],
            "empty": states["empty"],
            "not_returned": states["not_returned"],
            "unique": len(counts),
            "top_values_complete": len(counts) <= 20,
            "top_values": [
                {"value": value, "count": count,
                 "percent_of_records": round(100 * count / len(records), 2)}
                for value, count in counts.most_common(20)
            ],
        }
        if numeric:
            item["numeric"] = {
                "basis": "parseable_returned_value_not_field_storage_type",
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
    *, limit: int | None = 200,
) -> list[dict[str, Any]]:
    if not group_by:
        return []
    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    for record in records:
        fields = record.get("fields") or {}
        key = tuple((field_state(fields, name), _display_value(fields[name], max_chars=120)
                     if field_state(fields, name) == "value" else "") for name in group_by)
        item = groups.setdefault(
            key,
            {
                "group": {name: ({"_field_state": "not_returned"} if key[index][0] == "not_returned"
                                 else "（空）" if key[index][0] == "empty" else key[index][1])
                          for index, name in enumerate(group_by)},
                "group_field_states": {name: key[index][0] for index, name in enumerate(group_by)},
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
        item["percent_of_records"] = round(100 * item["count"] / len(records), 2)
        for name, numeric in list(item["numeric"].items()):
            if not numeric["count"]:
                item["numeric"].pop(name)
                continue
            numeric["sum"] = round(numeric["sum"], 6)
            numeric["average"] = round(numeric["sum"] / numeric["count"], 6)
        rows.append(item)
    rows.sort(key=lambda item: (-item["count"], _stable_json(item["group"])))
    return rows if limit is None else rows[:limit]


def _group_subtotals(
    records: list[dict[str, Any]],
    group_by: list[str],
    numeric_fields: list[str],
) -> list[dict[str, Any]]:
    """Compute first-dimension totals and conditional field counts locally."""
    if len(group_by) < 2:
        return []
    first = group_by[0]
    groups = {}
    for record in records:
        fields = record.get("fields") or {}
        state = field_state(fields, first)
        key = (state, _display_value(fields[first], max_chars=120) if state == "value" else "")
        groups.setdefault(key, []).append(record)
    rows = []
    for members in groups.values():
        item = _group_statistics(members, [first], numeric_fields)[0]
        item["percent_of_records"] = round(100 * len(members) / len(records), 2)
        item["field_statistics"] = _field_statistics(members, group_by[1:])
        rows.append(item)
    return sorted(rows, key=lambda item: (-item["count"], _stable_json(item["group"])))


def _bounded_groups(rows: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    kept, used = [], 2
    for item in rows[:200]:
        size = len(_stable_json(item)) + 1
        if used + size > max_chars:
            break
        kept.append(item)
        used += size
    return kept


def _compact_row(record: dict[str, Any], columns: list[str]) -> list[Any]:
    fields = record.get("fields") or {}
    return [
        record.get("id"),
        *[
            ({"_field_state": "not_returned"} if name not in fields else _display_value(fields[name], max_chars=2000))
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


def _analysis_fields(arguments: dict[str, Any]) -> list[str] | None:
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
    return fields


def analyze_record_snapshot(
    receipt: FetchReceipt,
    arguments: dict[str, Any],
    *,
    fields: list[str] | None = None,
    cache_ttl_seconds: int = _CACHE_TTL_SECONDS,
) -> dict[str, Any]:
    """Analyze an already authorized record snapshot without a WPS request."""
    note_read_event("local_snapshot_analysis")
    note_source_records(receipt.records)
    fields = fields or _analysis_fields(arguments)
    group_by = [
        str(item) for item in (arguments.get("group_by") or [])
        if str(item).strip()
    ]
    numeric_fields = [
        str(item) for item in (arguments.get("numeric_fields") or [])
        if str(item).strip()
    ]
    filtered, unknown = _apply_local_filters(
        receipt.records,
        arguments.get("local_filters"),
        str(arguments.get("local_filter_mode") or "AND"),
        with_unknown=True,
    )
    columns = _field_names(filtered, fields)
    include_rows = bool(arguments.get("include_rows", False))
    offset = max(0, int(arguments.get("row_offset") or 0))
    row_limit = max(1, min(int(arguments.get("row_limit") or 200), 1000))
    max_chars = max(4000, min(int(arguments.get("max_chars") or 60_000), 120_000))
    all_groups = _group_statistics(filtered, group_by, numeric_fields, limit=None)
    all_subtotals = _group_subtotals(filtered, group_by, numeric_fields)
    group_budget = min(20_000, max_chars // 2)
    groups = _bounded_groups(all_groups, group_budget)
    subtotals = _bounded_groups(all_subtotals, group_budget)
    statistics_complete = len(groups) == len(all_groups) and len(subtotals) == len(all_subtotals)
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
        "filter_unknown_records": unknown,
        "matched_records_is_exact": receipt.source_is_complete and unknown == 0,
        "pages_fetched": receipt.pages_fetched,
        "source_is_complete": receipt.source_is_complete,
        "continuation_error": receipt.continuation_error,
        "field_statistics": _field_statistics(filtered, columns),
        "group_statistics": groups,
        "group_subtotals": subtotals,
        "_statistics_scope": {
            "counted_records": len(filtered),
            "basis": "matched_records_in_source_snapshot",
            "group_count": len(all_groups), "returned_group_count": len(groups),
            "group_statistics_complete": len(groups) == len(all_groups),
            "subtotal_group_count": len(all_subtotals), "returned_subtotal_group_count": len(subtotals),
            "group_subtotals_complete": len(subtotals) == len(all_subtotals),
        },
        "columns": ["record_id", *columns],
        "rows": rows,
        "row_offset": offset,
        "returned_rows": len(rows),
        "next_row_offset": next_offset,
        "has_more": next_offset is not None,
        "is_complete": receipt.source_is_complete and unknown == 0 and statistics_complete and (not include_rows or (offset == 0 and next_offset is None)),
        "_record_scope": deepcopy(receipt.source_scope),
        "_analysis_scope": {"local_filters": deepcopy(arguments.get("local_filters") or []),
                            "local_filter_mode": str(arguments.get("local_filter_mode") or "AND"),
                            "source_records": len(receipt.records), "matched_records": len(filtered),
                            "filter_unknown_records": unknown},
        "_analysis_window": {"row_offset": offset, "returned_rows": len(rows), "next_row_offset": next_offset},
        "cache_ttl_seconds": cache_ttl_seconds,
        "_note": (
            "统计已基于确定匹配记录计算；分组结果仅部分展示。"
            if not statistics_complete else
            "统计基于全部匹配记录；紧凑记录尚有后续批次，请使用 next_row_offset 继续。"
            if next_offset is not None
            else "统计和所请求的紧凑记录均已完整返回。"
        ),
    }
    counts = record_counts(receipt.records, complete=receipt.source_is_complete, reported_total=receipt.query_total)
    result.update(source_total=counts["total"], source_total_is_known=counts["total_is_known"])
    if not receipt.source_is_complete:
        result["_note"] = (
            "源数据分页未能完整读取，不能据此声称分析完成。"
            f"原因：{receipt.continuation_error or '未知分页错误'}"
        )
    if unknown:
        result["_note"] = (f"已读范围中有 {len(filtered)} 条确定匹配；{unknown} 条源记录缺少筛选所需字段，"
                           "是否匹配尚不确定，不作为完整零条或全量统计。")
        if not receipt.source_is_complete:
            result["_note"] += " 源查询分页亦未完整读取。"
    if any(stat["not_returned"] for stat in result["field_statistics"].values()):
        result["_note"] += " not_returned 和 _field_state 表示未返回；empty 只计明确返回的空值。"
    if not statistics_complete:
        result["_note"] += " 分组或小计未完整输出；缺席的组不等于零，可按分组字段加local_filters缩小范围后再分析。"
    return result


def analyze_cached_records(
    source: dict[str, Any],
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Analyze the fetched portion of one same-turn list_records receipt."""
    records = source.get("records")
    if not valid_page(source):
        return {"error": "该记录结果不可分析"}
    filters = arguments.get("local_filters") or []
    if not isinstance(filters, list) or any(not isinstance(item, dict) for item in filters):
        return {"error": "local_filters 必须是条件对象数组"}
    valid_operators = {
        "eq", "ne", "contains", "not_contains", "in",
        "gt", "gte", "lt", "lte", "between", "empty", "not_empty",
    }
    if any(
        not str(item.get("field") or "").strip()
        or str(item.get("operator") or "").lower() not in valid_operators
        for item in filters
    ):
        return {"error": "筛选条件必须包含有效的 field 和 operator"}
    known_fields = {
        name
        for record in records if isinstance(record, dict)
        for name in (record.get("fields") or {})
    }
    unknown = sorted({
        str(item["field"])
        for item in filters
        if str(item["field"]) not in known_fields
        and str(item["operator"]).lower() not in {"empty", "not_empty"}
    }) if records else []
    complete = (exhausted(source) and source.get("is_complete") is not False
                and not (source.get("_record_scope") or {}).get("start_token"))
    continuation_error = ""
    if not complete:
        if source.get("has_more"):
            token = source.get("next_page_token")
            continuation_error = (
                f"本轮已读结果仍有 WPS 下一页，next_page_token={token!r}"
                if token else "源数据仍有 WPS 下一页，但当前回执缺少游标"
            )
        else:
            continuation_error = str(
                source.get("continuation_error") or "本轮源数据不完整"
            )
    receipt = FetchReceipt(
        records=records,
        pages_fetched=(
            int(source["pages_fetched"])
            if source.get("pages_fetched") is not None else None
        ),
        source_is_complete=complete,
        continuation_error=continuation_error,
        source_scope=deepcopy(source.get("_record_scope")),
        query_total=source.get("total") if source.get("total_is_known", source.get("total") is not None) else None,
    )
    result = analyze_record_snapshot(
        receipt, arguments, cache_ttl_seconds=0,
    )
    if unknown:
        result.update(not_returned_filter_fields=unknown, available_fields=sorted(known_fields)[:50])
    if not result["matched_records"] and not result["filter_unknown_records"] and records and filters:
        result["filter_field_samples"] = {
            field: list(dict.fromkeys(
                shown
                for record in records
                if (shown := _display_value(
                    (record.get("fields") or {}).get(field), max_chars=80,
                ))
            ))[:3]
            for field in dict.fromkeys(str(item["field"]) for item in filters)
        }
        result["_note"] += " 已读记录中无匹配；请核对筛选值格式与字段示例。"
    return result


async def analyze_records(
    access_token: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    fields = _analysis_fields(arguments)
    receipt = await _fetch_all_records(
        access_token,
        str(arguments["file_id"]),
        int(arguments["sheet_id"]),
        fields=fields,
        server_filter=arguments.get("filter"),
        view_id=arguments.get("view_id"),
    )
    return analyze_record_snapshot(receipt, arguments, fields=fields)
