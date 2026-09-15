"""任意 WPS 多维表格的只读分页缓存。"""

from __future__ import annotations

import asyncio

from agent.wps_client import get_schema, list_records
from auth import db
from core.dashboard_cache import _SYNC_LOCKS, _sheet_id, _sheet_name, _token
from core.dashboard_service import _schema_sheets


_SHEET_LIMIT = 5000
GENERIC_CACHE_VERSION = 4


def _sheet_type(sheet: dict) -> str:
    return str(sheet.get("type") or sheet.get("sheet_type") or sheet.get("kind") or "")


def _is_record_sheet(sheet: dict) -> bool:
    """WPS 自由纸张等说明页存在于 schema，但没有 records 接口。"""
    sheet_type = _sheet_type(sheet).lower()
    if not sheet_type:
        # 兼容旧响应和测试桩：类型缺失时仍尝试读取。
        return True
    # schema 还会混入仪表盘、自由纸张等页面，它们没有 records 接口。
    return "databasesheet" in sheet_type


async def _load_sheet(token: str, file_id: str, sheet: dict) -> dict:
    """分页读取真实记录；缺游标或达到安全上限时显式标注不完整。"""
    records: list[dict] = []
    next_token: str | None = None
    total: int | None = None
    complete = False
    while len(records) < _SHEET_LIMIT:
        response = await list_records(
            token, file_id, _sheet_id(sheet), page_size=1000, max_records=1000,
            page_token=next_token,
        )
        page = response.get("records") or []
        records.extend(page)
        raw_total = response.get("total")
        if raw_total is not None:
            try:
                total = int(raw_total)
            except (TypeError, ValueError):
                pass
        if not response.get("has_more"):
            complete = True
            break
        following = response.get("next_page_token")
        if not following or following == next_token or not page:
            break
        next_token = following
    schema_count = sheet.get("records_count")
    if schema_count is not None:
        try:
            schema_total = int(schema_count)
            total = max(total or 0, schema_total)
            if schema_total > len(records):
                complete = False
        except (TypeError, ValueError):
            pass
    if total is not None and total > len(records):
        complete = False
    return {"records": records[:_SHEET_LIMIT], "total": total, "complete": complete}


async def sync_generic_dashboard_cache(user_id: int, file_id: str) -> dict:
    """同步所选文件的每个工作表，独立于旧的日报/任务/项目仓库。"""
    lock = _SYNC_LOCKS.setdefault((user_id, file_id), asyncio.Lock())
    async with lock:
        token = await _token(user_id)
        schema = await get_schema(token, file_id)
        schema_sheets = [sheet for sheet in _schema_sheets(schema) if _sheet_id(sheet) is not None]
        sheets = [sheet for sheet in schema_sheets if _is_record_sheet(sheet)]
        skipped = [{
            "id": str(_sheet_id(sheet)),
            "name": _sheet_name(sheet) or str(_sheet_id(sheet)),
            "type": _sheet_type(sheet),
            "reason": "非数据工作表，不提供记录接口",
        } for sheet in schema_sheets if not _is_record_sheet(sheet)]
        if not sheets:
            raise RuntimeError("所选文件没有可分析的数据工作表（说明页不提供记录接口）")
        semaphore = asyncio.Semaphore(3)

        async def one(sheet: dict) -> dict:
            async with semaphore:
                return await _load_sheet(token, file_id, sheet)

        results = await asyncio.gather(*(one(sheet) for sheet in sheets), return_exceptions=True)
        errors = [f"{_sheet_name(sheet)}: {type(result).__name__}"
                  for sheet, result in zip(sheets, results) if isinstance(result, Exception)]
        if errors:
            # 任一工作表失败就不更新索引，避免把不完整数据冒充全部文件。
            raise RuntimeError("工作表读取失败：" + "、".join(errors))
        index_sheets = []
        for sheet, result in zip(sheets, results):
            sheet_id = str(_sheet_id(sheet))
            db.save_dashboard_data_cache(user_id, file_id, f"generic_sheet:{sheet_id}", result)
            index_sheets.append({
                "id": sheet_id, "name": _sheet_name(sheet) or sheet_id,
                "total": result["total"], "complete": result["complete"],
            })
        index = {
            "version": GENERIC_CACHE_VERSION, "sheets": index_sheets,
            "schema_sheets": sheets, "skipped_sheets": skipped, "records": [],
        }
        db.save_dashboard_data_cache(user_id, file_id, "generic_index", index)
        return {
            "ok": True, "updated": {sheet["name"]: len(result["records"])
                                     for sheet, result in zip(index_sheets, results)},
            "skipped": skipped, "errors": [],
        }
