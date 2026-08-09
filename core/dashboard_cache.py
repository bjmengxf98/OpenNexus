"""驾驶舱 WPS → SQLite 本地数据仓库同步。"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

from agent.wps_client import get_schema, list_records
from auth import db
from auth.wps_oauth import auto_refresh_token_for_user, is_token_expiring_soon


_SYNC_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}


def _sheet_name(sheet: dict) -> str:
    return str(sheet.get("name") or sheet.get("title") or "")


def _sheet_id(sheet: dict) -> Any:
    return sheet.get("id") or sheet.get("sheet_id") or sheet.get("sheetId")


def _sheets(schema: dict) -> list[dict]:
    value = schema.get("sheets", []) if isinstance(schema, dict) else []
    return value if isinstance(value, list) else []


def _pick(sheets: list[dict], kind: str) -> dict | None:
    scored = []
    for sheet in sheets:
        name = _sheet_name(sheet)
        score = 0
        if kind == "daily":
            score = 100 if "每日进展" in name else (60 if "进展" in name else 0)
        elif kind == "tasks":
            score = 100 if name in {"任务", "任务表", "部门任务"} else (
                60 if "任务" in name and not any(x in name for x in ("每日", "进展", "子任务")) else 0
            )
        elif kind == "projects":
            score = 100 if name in {"项目", "项目表", "部门项目"} else (60 if "项目" in name else 0)
        elif kind == "people":
            score = 80 if any(x in name for x in ("人员信息", "部门人员", "成员", "通讯录")) else 0
        if score:
            scored.append((score, sheet))
    return max(scored, key=lambda item: item[0])[1] if scored else None


async def _token(user_id: int) -> str:
    row = db.get_wps_token(user_id)
    if not row or not row.get("access_token"):
        raise RuntimeError("尚未连接 WPS")
    if is_token_expiring_soon(row.get("expires_at") or "2000-01-01", minutes=5):
        if not await auto_refresh_token_for_user(user_id):
            raise RuntimeError("WPS 授权已过期，请重新连接")
        row = db.get_wps_token(user_id)
    return row.get("access_token", "")


def _view_id(sheet: dict) -> str | None:
    views = sheet.get("views") or []
    if not views:
        return None
    # 优先记录数最多的主视图，避免个人过滤视图。
    view = max(views, key=lambda item: int(item.get("records_count") or 0))
    return view.get("id")


async def _load_all(token: str, file_id: str, sheet: dict) -> list[dict]:
    count = int(sheet.get("records_count") or 1000)
    limit = max(1, min(count, 1000))
    result = await list_records(
        token, file_id, _sheet_id(sheet), page_size=1000,
        max_records=limit, view_id=_view_id(sheet),
    )
    return result.get("records", []) if isinstance(result, dict) else []


async def _load_daily_dates(
    token: str,
    file_id: str,
    sheet: dict,
    dates: list[date],
) -> list[dict]:
    async def one(target: date) -> list[dict]:
        result = await list_records(
            token, file_id, _sheet_id(sheet), page_size=500, max_records=500,
            filter={
                "mode": "AND",
                "criteria": [{
                    "field": "填报日期", "operator": "Equals",
                    "values": [target.strftime("%Y/%m/%d")],
                }],
            },
        )
        return result.get("records", []) if isinstance(result, dict) else []

    batches = await asyncio.gather(*(one(item) for item in sorted(set(dates))))
    return [record for batch in batches for record in batch]


async def _load_daily_full(
    token: str,
    file_id: str,
    sheet: dict,
    extra_dates: list[date] | None = None,
) -> list[dict]:
    records = await _load_all(token, file_id, sheet)
    if int(sheet.get("records_count") or 0) > 1000:
        # 超过接口单页上限后，用近日日增量补齐尾部，历史部分继续由本地仓库保留。
        recent_dates = extra_dates or [date.today(), date.today() - timedelta(days=1)]
        recent = await _load_daily_dates(token, file_id, sheet, recent_dates)
        records = _merge(records, recent)
    return records


def _merge(existing: list[dict], incoming: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    anonymous = 0
    for record in existing + incoming:
        key = str(record.get("id") or record.get("_id") or "")
        if not key:
            anonymous += 1
            key = f"__anonymous_{anonymous}"
        merged[key] = record
    return list(merged.values())


async def sync_dashboard_cache(
    user_id: int,
    file_id: str,
    *,
    full: bool = False,
    target_dates: list[date] | None = None,
) -> dict:
    """同步驾驶舱数据；首次全量，之后只增量拉取近日日报。"""
    key = (user_id, file_id)
    lock = _SYNC_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        access_token = await _token(user_id)
        schema = await get_schema(access_token, file_id)
        sheets = _sheets(schema)
        if not sheets:
            raise RuntimeError("WPS 未返回工作表结构")
        db.save_dashboard_data_cache(user_id, file_id, "schema", {"schema": schema, "records": []})

        existing_daily = db.get_dashboard_data_cache(user_id, file_id, "daily")
        daily_sheet = _pick(sheets, "daily")
        jobs: list[tuple[str, dict, Any]] = []
        if daily_sheet:
            if full or not existing_daily:
                jobs.append((
                    "daily", daily_sheet,
                    _load_daily_full(access_token, file_id, daily_sheet, target_dates),
                ))
            else:
                dates = target_dates or [date.today(), date.today() - timedelta(days=1)]
                jobs.append(("daily_incremental", daily_sheet, _load_daily_dates(access_token, file_id, daily_sheet, dates)))

        for kind in ("people", "tasks", "projects"):
            sheet = _pick(sheets, kind)
            if sheet and (full or not db.get_dashboard_data_cache(user_id, file_id, kind)):
                jobs.append((kind, sheet, _load_all(access_token, file_id, sheet)))

        results = await asyncio.gather(*(job[2] for job in jobs), return_exceptions=True)
        summary = {"ok": True, "updated": {}, "errors": []}
        for (kind, sheet, _), result in zip(jobs, results):
            if isinstance(result, Exception):
                summary["errors"].append(f"{kind}: {result}")
                continue
            store_kind = "daily" if kind == "daily_incremental" else kind
            records = result
            if kind == "daily_incremental":
                records = _merge((existing_daily or {}).get("records", []), result)
            elif kind == "daily" and existing_daily and int(sheet.get("records_count") or 0) > 1000:
                records = _merge(existing_daily.get("records", []), result)
            db.save_dashboard_data_cache(user_id, file_id, store_kind, {
                "sheet_id": _sheet_id(sheet),
                "sheet_name": _sheet_name(sheet),
                "records": records,
            })
            summary["updated"][store_kind] = len(records)
        summary["ok"] = not summary["errors"]
        return summary


def cached_daily_dates(user_id: int, file_id: str) -> list[str]:
    from core.dashboard_service import DATE_ALIASES, _field_date, _record_fields

    cached = db.get_dashboard_data_cache(user_id, file_id, "daily") or {}
    values = {
        parsed.isoformat()
        for record in cached.get("records", [])
        if (parsed := _field_date(_record_fields(record), DATE_ALIASES))
    }
    return sorted(values, reverse=True)
