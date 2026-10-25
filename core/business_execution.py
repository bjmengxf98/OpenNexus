"""Small execution contracts shared by local chat and remote MCP clients.

No model calls or conversation state live here. WPS credentials and configured
files must come from the authenticated caller, never from model-supplied storage
targets. Record verification performs reads only; it never retries a mutation.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Callable

from agent.record_analyzer import invalidate_record_cache

_VERIFY_MAX_PAGES = 5
_VERIFY_TIMEOUT_SECONDS = 20
_NO_RETRY = "请先读取目标记录核对；不要自动重做写入，以免重复创建或扩大修改范围。"


def document_upload_anchors(default_file: dict | None, files: list | None) -> list[str]:
    return list(dict.fromkeys(
        str(item.get("file_id")) for item in [default_file, *(files or [])]
        if isinstance(item, dict) and item.get("file_id")
    ))


def prepare_document_arguments(arguments: dict, anchors: list[str]) -> dict:
    if not anchors:
        raise ValueError(
            "当前用户尚未配置可用于定位本人 WPS 网盘的文件，"
            "请先在设置中添加一个由本人创建的 WPS 文件。"
        )
    return {**arguments, "dbsheet_file_id": anchors[0],
            "_upload_anchor_file_ids": list(anchors)}


def _record_id(record: dict) -> str:
    return str(record.get("id") or record.get("record_id") or "")


def _same_value(expected, actual) -> bool:
    # Contact/Link responses may add display names; compare the supplied stable
    # identity, not a possibly stale nickname. Do not guess conversions of text,
    # dates or formulas: unsupported representations remain unverified.
    if isinstance(expected, dict) and isinstance(actual, dict):
        if expected.get("id") is not None:
            return str(expected["id"]) == str(actual.get("id"))
        return all(key in actual and _same_value(value, actual[key])
                   for key, value in expected.items())
    if isinstance(expected, list) and isinstance(actual, list):
        remaining = list(actual)
        for value in expected:
            for index, candidate in enumerate(remaining):
                if _same_value(value, candidate):
                    remaining.pop(index)
                    break
            else:
                return False
        return not remaining
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    return expected == actual


def record_values_match(expected: dict, actual: dict) -> bool:
    fields = actual.get("fields")
    if not isinstance(fields, dict):
        return False
    return all(
        key in fields and _same_value(value, fields[key])
        for key, value in expected.items() if key not in {"id", "record_id"}
    )


async def _read_targets(read: Callable, token: str, arguments: dict,
                        target_ids: list[str] | None, fields: list[str]) -> tuple[dict, bool, int]:
    found, seen_tokens = {}, set()
    page_token = ""  # Explicit token selects the existing reader's single-page path.
    for page_number in range(1, _VERIFY_MAX_PAGES + 1):
        page = await read(token, arguments["file_id"], arguments["sheet_id"],
                          page_size=1000, page_token=page_token, fields=fields or None)
        if (not isinstance(page, dict) or page.get("error") or page.get("ok") is False
                or page.get("code", 0) not in (0, "0")
                or not isinstance(page.get("records"), list)):
            raise ValueError("写后读取没有返回有效记录页")
        for record in page["records"]:
            if not isinstance(record, dict) or not _record_id(record):
                raise ValueError("写后读取缺少记录身份，不能据此判断目标是否存在")
            if target_ids is None or _record_id(record) in target_ids:
                found[_record_id(record)] = record
        complete = not page.get("has_more") and page.get("is_complete") is not False
        if complete or (target_ids is not None and len(found) == len(target_ids)):
            return found, complete, page_number
        next_token = page.get("next_page_token")
        if not next_token or str(next_token) in seen_tokens:
            return found, False, page_number
        page_token = str(next_token)
        seen_tokens.add(page_token)
    return found, False, _VERIFY_MAX_PAGES


async def execute_record_write(name: str, token: str, arguments: dict,
                               *, write: Callable, read: Callable) -> dict:
    args = deepcopy(arguments)
    is_delete = name == "delete_records"
    records = args.get("records") or []
    if not isinstance(records, list):
        return {"error": "records 必须是数组，未提交任何写入。", "write_submitted": False}
    ids = (args.get("record_ids") or []) if is_delete else [
        _record_id(record) if isinstance(record, dict) else "" for record in records
    ]
    if is_delete:
        valid = isinstance(ids, list) and bool(ids) and all(isinstance(i, str) and i for i in ids)
    else:
        valid = isinstance(records, list) and bool(records) and all(
            isinstance(record, dict) and any(k not in {"id", "record_id"} for k in record)
            for record in records
        )
        if name == "update_records":
            valid = valid and all(ids) and len(set(ids)) == len(ids)
    if not valid:
        return {"error": "记录参数为空、缺少有效编号或字段，未提交任何写入。", "write_submitted": False}

    fields = list(dict.fromkeys(key for record in records for key in record
                               if key not in {"id", "record_id"})) if not is_delete else []
    before_ids = None
    if name == "create_records":
        # Some WPS versions acknowledge creates without new IDs. A complete
        # before/after identity delta can verify them without matching an old row.
        try:
            async with asyncio.timeout(_VERIFY_TIMEOUT_SECONDS):
                before, complete, _ = await _read_targets(read, token, args, None, fields)
            if complete:
                before_ids = set(before)
        except Exception:
            pass  # A read failure does not grant permission to retry the mutation.

    try:
        receipt = await write(token, args["file_id"], args["sheet_id"], ids if is_delete else records)
    except Exception:
        # A transport failure can happen after WPS commits. Do not imply retry safety.
        invalidate_record_cache(args["file_id"], args["sheet_id"])
        return {"ok": False, "write_submitted": None, "verified": False,
                "verification": {"status": "submission_unknown"},
                "message": "写入请求异常，是否已生效未知。" + _NO_RETRY}
    if not isinstance(receipt, dict):
        receipt = {"result": receipt}
    if (receipt.get("error") or receipt.get("ok") is False
            or receipt.get("code", 0) not in (0, "0")):
        if not receipt.get("error"):
            receipt["error"] = "WPS 未接受该写入请求。"
        return {**receipt, "verified": False}

    invalidate_record_cache(args["file_id"], args["sheet_id"])
    result = {**receipt, "ok": False, "write_submitted": True, "verified": False}
    # Raw write hints must not encourage repeating an already submitted mutation.
    result.pop("_verify_hint", None)
    result["message"] = "写入请求已提交，但尚未核验生效。" + _NO_RETRY
    if name == "create_records":
        returned = receipt.get("records") or []
        ids = [_record_id(record) for record in returned if isinstance(record, dict)]
        if len(ids) != len(records) or not all(ids) or len(set(ids)) != len(ids):
            if before_ids is None:
                result["verification"] = {"status": "missing_created_ids", "target_ids": []}
                return result
            ids = None
    if ids is not None:
        ids = list(dict.fromkeys(ids))
    try:
        async with asyncio.timeout(_VERIFY_TIMEOUT_SECONDS):
            found, complete, pages = await _read_targets(read, token, args, ids, fields)
    except Exception:
        result["verification"] = {"status": "readback_unavailable", "target_ids": ids or []}
        return result

    if ids is None:
        found = {rid: record for rid, record in found.items() if rid not in before_ids}
        ids = list(found) if complete and len(found) == len(records) else []
        if not ids:
            result["verification"] = {"status": "created_ids_ambiguous", "target_ids": [],
                                      "source_is_complete": complete, "pages_read": pages}
            return result
        result["created_ids"] = ids

    if is_delete:
        verified = complete and not found
    elif name == "update_records":
        verified = all(rid in found and record_values_match(record, found[rid])
                       for rid, record in zip(ids, records))
    else:
        # WPS need not preserve create response order. Match the complete batch,
        # consuming each returned identity once; no pre-existing row can stand in.
        unmatched = list(records)
        for rid in ids:
            actual = found.get(rid)
            for index, expected in enumerate(unmatched):
                if actual and record_values_match(expected, actual):
                    unmatched.pop(index)
                    break
        verified = not unmatched
    result.update(ok=verified, verified=verified)
    result["verification"] = {
        "status": "verified" if verified else "not_confirmed",
        "target_ids": ids, "source_is_complete": complete, "pages_read": pages,
        "observed_records": list(found.values()),
    }
    result["message"] = (
        "已回读确认目标记录不存在（不代表这些编号此前一定存在）。" if is_delete else
        "已回读确认目标记录和所提交字段一致。"
    ) if verified else "写入请求已提交，但回读结果未能确认全部目标生效。" + _NO_RETRY
    return result
