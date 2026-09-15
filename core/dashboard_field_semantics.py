"""WPS 字段的通用语义、显示值解码和跨表关联名称解析。"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Callable

from core.dashboard_service import _plain, _record_fields


_IDENTIFIER_NAME = re.compile(
    r"(^|[_\s-])id($|[_\s-])|id$|编号|序号|编码|主键|账号|账户|工号|证件|手机号|电话|邮编",
    re.I,
)
_LABEL_NAME = re.compile(r"姓名|名称|标题|联系人|负责人|项目|任务|部门|岗位|专业|name|title", re.I)
_LINK_TYPES = {"onewaylink", "twowaylink", "link", "relation"}
_CONTACT_TYPES = {"contact", "user", "member", "people"}
_HIERARCHY_TYPES = {"cascade", "department"}
_SELECT_TYPES = {"singleselect", "multiselect", "select"}


def _value_for_keys(value: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        if value.get(key) not in (None, "", []):
            return value[key]
    return None


def sheet_id(sheet: dict) -> str:
    for key in ("id", "sheet_id", "sheetId"):
        if sheet.get(key) is not None:
            return str(sheet[key])
    return ""


def field_name(field: dict) -> str:
    return str(field.get("name") or field.get("title") or field.get("label") or "").strip()


def field_type(field: dict | None) -> str:
    field = field or {}
    return str(field.get("type") or field.get("value_type") or field.get("field_type") or "").strip()


def is_identifier_field(name: str, field: dict | None = None) -> bool:
    """标识符可展示和去重，但不能作为业务数值做求和、平均或分布。"""
    del field
    return bool(_IDENTIFIER_NAME.search(str(name or "")))


def is_numeric_field(field: dict | None) -> bool:
    """只信任 schema 声明的数值类型，不把数字形态的文本/ID当成数值。"""
    field = field or {}
    kind = field_type(field).lower()
    if any(token in kind for token in ("number", "numeric", "integer", "float", "decimal", "currency", "percent", "rating")):
        return True
    if kind == "formula":
        data = field.get("data") if isinstance(field.get("data"), dict) else {}
        value_type = str(data.get("value_type") or data.get("valueType") or "").lower()
        return "number" in value_type or "numeric" in value_type
    return False


def field_definitions(index: dict) -> dict[str, dict[str, dict]]:
    result: dict[str, dict[str, dict]] = {}
    for sheet in index.get("schema_sheets") or []:
        definitions = {}
        for field in sheet.get("fields") or sheet.get("columns") or []:
            name = field_name(field)
            if name:
                definitions[name] = field
        result[sheet_id(sheet)] = definitions
    return result


def _parse_json_value(value: Any) -> Any:
    if isinstance(value, str) and value.strip().startswith(("[", "{")):
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return value
    return value


def _hierarchy_text(value: Any) -> str:
    value = _parse_json_value(value)
    if isinstance(value, list):
        return "、".join(dict.fromkeys(text for item in value if (text := _hierarchy_text(item))))
    if isinstance(value, dict):
        districts = value.get("districts")
        if isinstance(districts, list):
            return " / ".join(_plain(item) for item in districts if _plain(item))
        nested = _value_for_keys(value, ("value", "values", "text", "label", "name"))
        return _hierarchy_text(nested) if nested is not None else ""
    return _plain(value)


def _contact_text(value: Any) -> str:
    value = _parse_json_value(value)
    if isinstance(value, list):
        return "、".join(dict.fromkeys(text for item in value if (text := _contact_text(item))))
    if isinstance(value, dict):
        visible = _value_for_keys(value, ("nickName", "nickname", "display_name", "name", "text", "label"))
        return _plain(visible) if visible is not None else ""
    # 纯数字或短编码是成员内部 ID，不直接暴露为联系人名称。
    text = _plain(value)
    return "" if re.fullmatch(r"[A-Za-z0-9_-]+", text or "") else text


def _linked_ids(value: Any) -> list[str]:
    value = _parse_json_value(value)
    if isinstance(value, list):
        return [item for value_item in value for item in _linked_ids(value_item)]
    if isinstance(value, dict):
        nested = _value_for_keys(value, ("record_id", "recordId", "id", "value", "values"))
        return _linked_ids(nested) if nested is not None else []
    text = str(value or "").strip()
    return [text] if text else []


def display_field_value(value: Any, field: dict | None = None, link_lookup: dict[str, str] | None = None) -> str:
    """把 WPS 选择、人员、层级和关联字段转换为用户可读文本。"""
    field = field or {}
    kind = field_type(field).lower()
    link_lookup = link_lookup or {}
    if kind in _HIERARCHY_TYPES:
        return _hierarchy_text(value)
    if kind in _CONTACT_TYPES:
        return _contact_text(value)
    if kind in _LINK_TYPES:
        labels = [link_lookup.get(item, "") for item in _linked_ids(value)]
        return "、".join(dict.fromkeys(label for label in labels if label))
    if kind in _SELECT_TYPES:
        data = field.get("data") if isinstance(field.get("data"), dict) else {}
        options = {
            str(item.get("id")): _plain(item.get("value"))
            for item in data.get("items") or []
            if isinstance(item, dict) and item.get("id") is not None
        }
        raw = _parse_json_value(value)
        if isinstance(raw, list):
            values = [display_field_value(item, field, link_lookup) for item in raw]
            return "、".join(dict.fromkeys(item for item in values if item))
        if isinstance(raw, dict):
            raw = _value_for_keys(raw, ("value", "text", "label", "name", "id"))
        text = _plain(raw)
        return options.get(text, text)
    return _plain(value)


def _record_id(record: dict) -> str:
    for key in ("id", "record_id", "recordId"):
        if record.get(key) is not None:
            return str(record[key])
    return ""


def _record_label(record: dict, definitions: dict[str, dict]) -> str:
    candidates: list[tuple[int, str]] = []
    for name, value in _record_fields(record).items():
        name = str(name)
        if is_identifier_field(name) or not _LABEL_NAME.search(name):
            continue
        field = definitions.get(name, {})
        if field_type(field).lower() in _LINK_TYPES:
            continue
        text = display_field_value(value, field)
        if not text or len(text) > 80 or re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
            continue
        normalized = re.sub(r"[\s_（）()·.-]", "", name).lower()
        if normalized in {"姓名", "人员姓名", "项目名称", "任务名称", "部门名称", "标题", "名称", "name", "title"}:
            score = 100
        elif re.search(r"姓名|名称|标题|name|title", name, re.I):
            score = 90
        elif re.search(r"联系人|负责人", name):
            score = 70
        else:
            score = 50
        candidates.append((score, text))
    return max(candidates, default=(0, ""), key=lambda item: item[0])[1]


def build_value_context(
    index: dict,
    load_sheet: Callable[[str], dict | None],
) -> tuple[dict[str, dict[str, dict]], dict[str, str]]:
    """建立各表字段定义与文件内唯一的关联记录显示名称映射。"""
    definitions = field_definitions(index)
    candidates: defaultdict[str, set[str]] = defaultdict(set)
    for sheet in index.get("sheets") or []:
        sid = str(sheet.get("id") or "")
        cached = load_sheet(sid) or {}
        for record in cached.get("records") or []:
            record_id = _record_id(record)
            label = _record_label(record, definitions.get(sid, {}))
            if record_id and label:
                candidates[record_id].add(label)
    lookup = {record_id: next(iter(labels)) for record_id, labels in candidates.items() if len(labels) == 1}
    return definitions, lookup
