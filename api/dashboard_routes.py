"""部门驾驶舱页面与 JSON API。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from auth import db
from core.dashboard_service import DashboardError, VALID_VIEWS, generate_dashboard
from core.dashboard_cache import cached_daily_dates, sync_dashboard_cache


dashboard_router = APIRouter()
_HTML_FILE = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"


def _current_user(request: Request) -> dict | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    row = db.get_user_by_id(uid)
    user = dict(row) if row else None
    if not user or not user.get("is_enabled", True):
        return None
    return user


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": "登录状态已失效，请重新登录"}, status_code=401
    )


@dashboard_router.get("/dashboard")
async def dashboard_page(request: Request):
    if not _current_user(request):
        return RedirectResponse("/login?next=/dashboard", status_code=302)
    if not _HTML_FILE.exists():
        return JSONResponse({"ok": False, "error": "驾驶舱页面文件不存在"}, status_code=500)
    return FileResponse(_HTML_FILE, media_type="text/html; charset=utf-8")


@dashboard_router.get("/api/dashboard/files")
async def dashboard_files(request: Request):
    user = _current_user(request)
    if not user:
        return _unauthorized()
    files = db.list_wps_files(user["id"])
    default_file = db.get_default_wps_file(user["id"])
    default_id = default_file.get("file_id") if default_file else ""
    return JSONResponse(
        {
            "ok": True,
            "user": {"id": user["id"], "name": user.get("display_name") or user.get("username")},
            "default_file_id": default_id,
            "files": [
                {
                    "file_id": item.get("file_id", ""),
                    "file_name": item.get("file_name") or item.get("name") or item.get("file_id", ""),
                    "is_default": item.get("file_id") == default_id,
                }
                for item in files
            ],
        }
    )


@dashboard_router.get("/api/dashboard/data")
async def dashboard_data(
    request: Request,
    view: str = Query("overview"),
    snapshot_date: str = Query("", alias="date"),
    file_id: str = Query(""),
    refresh: bool = Query(False),
    ai_summary: bool = Query(False),
):
    user = _current_user(request)
    if not user:
        return _unauthorized()
    if view not in VALID_VIEWS:
        return JSONResponse({"ok": False, "error": "不支持的驾驶舱页面"}, status_code=400)

    files = db.list_wps_files(user["id"])
    if not file_id:
        default_file = db.get_default_wps_file(user["id"])
        file_id = default_file.get("file_id", "") if default_file else ""
    if not file_id or not any(item.get("file_id") == file_id for item in files):
        return JSONResponse(
            {"ok": False, "error": "请先在设置中添加并选择 WPS 多维表格"}, status_code=400
        )

    target = snapshot_date or date.today().isoformat()
    try:
        if refresh:
            try:
                target_date = date.fromisoformat(target)
            except ValueError:
                target_date = date.today()
            await sync_dashboard_cache(
                user["id"], file_id, full=True, target_dates=[target_date]
            )
        payload = await generate_dashboard(
            user["id"], file_id, view, target,
            force=refresh or ai_summary, use_ai=ai_summary
        )
    except DashboardError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"驾驶舱生成失败：{exc}"}, status_code=500
        )
    return JSONResponse({"ok": True, "data": payload})


@dashboard_router.get("/api/dashboard/dates")
async def dashboard_dates(
    request: Request,
    file_id: str = Query(""),
    view: str = Query("daily"),
):
    user = _current_user(request)
    if not user:
        return _unauthorized()
    if view not in VALID_VIEWS:
        return JSONResponse({"ok": False, "error": "不支持的驾驶舱页面"}, status_code=400)
    if not file_id:
        default_file = db.get_default_wps_file(user["id"])
        file_id = default_file.get("file_id", "") if default_file else ""
    dates = cached_daily_dates(user["id"], file_id) if file_id and view == "daily" else (
        db.list_dashboard_snapshot_dates(user["id"], file_id, view) if file_id else []
    )
    return JSONResponse({"ok": True, "dates": dates})


@dashboard_router.get("/api/dashboard/cache_status")
async def dashboard_cache_status(request: Request, file_id: str = Query("")):
    user = _current_user(request)
    if not user:
        return _unauthorized()
    if not file_id:
        default_file = db.get_default_wps_file(user["id"])
        file_id = default_file.get("file_id", "") if default_file else ""
    return JSONResponse({
        "ok": True,
        "items": db.get_dashboard_cache_status(user["id"], file_id) if file_id else [],
    })
