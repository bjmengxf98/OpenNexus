"""独立 HTML 设置页及其 JSON API。

所有状态都通过 Starlette Session 确定当前用户。
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from agent.assistant import LLM_PRESETS
from auth import db


settings_new_router = APIRouter()
_HTML_FILE = Path(__file__).resolve().parent.parent / "static" / "settings_new.html"


def _current_user(request: Request):
    uid = request.session.get("uid")
    if not uid:
        return None, None
    row = db.get_user_by_id(int(uid))
    if not row or not row["is_enabled"]:
        request.session.clear()
        return None, None
    return int(uid), dict(row)


def _not_logged_in():
    return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)


def _extract_file_id(raw: str) -> str:
    raw = str(raw or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{8,}", raw):
        return raw
    for pattern in (
        r"/l/([A-Za-z0-9_-]+)",
        r"/p/([A-Za-z0-9_-]+)",
        r"file/([A-Za-z0-9_-]+)",
        r"fileId=([A-Za-z0-9_-]+)",
        r"id=([A-Za-z0-9_-]+)",
    ):
        match = re.search(pattern, raw)
        if match:
            return match.group(1)
    return ""


def _preset_payload():
    result = {}
    for key, value in LLM_PRESETS.items():
        result[key] = {
            "name": value.get("name", key),
            "base_url": value.get("base_url", ""),
            "model": value.get("model", ""),
            "models": [
                {"id": item.get("id", ""), "name": item.get("name", item.get("id", ""))}
                for item in value.get("models", [])
            ],
        }
    return result


@settings_new_router.get("/settings", response_class=HTMLResponse)
@settings_new_router.get("/settings-new", response_class=HTMLResponse)
async def settings_new_page(request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return RedirectResponse("/login?next=" + request.url.path, status_code=302)
    return HTMLResponse(_HTML_FILE.read_text(encoding="utf-8"))


@settings_new_router.get("/api/settings/bootstrap")
async def settings_bootstrap(request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return _not_logged_in()
    token = db.get_wps_token(uid) or {}
    return {
        "ok": True,
        "user": {
            "id": uid,
            "username": user.get("username", ""),
            "display_name": user.get("display_name") or "",
            "role": user.get("role", "staff"),
            "wecom_userid": user.get("wecom_userid") or "",
        },
        "wps_files": db.list_wps_files(uid),
        "wps_account_id": token.get("wps_account_id") or "",
        "llm": db.get_llm_key(uid) or {},
        "image_llm": db.get_image_llm_key(uid) or {},
        "presets": _preset_payload(),
        "personal_weixin_id": db.get_personal_weixin_id(uid) or "",
        "mcp": {
            "endpoint": str(request.base_url).rstrip("/") + "/mcp/",
            "tokens": db.list_mcp_tokens(uid),
        },
    }


@settings_new_router.get("/api/settings/provider/{kind}/{provider}")
async def settings_provider(kind: str, provider: str, request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return _not_logged_in()
    if provider not in LLM_PRESETS:
        return JSONResponse({"ok": False, "error": "不支持的模型提供商"}, status_code=400)
    if kind == "main":
        config = db.get_provider_config(uid, provider) or {}
    elif kind == "image":
        config = db.get_image_provider_config(uid, provider) or {}
    else:
        return JSONResponse({"ok": False, "error": "配置类型错误"}, status_code=400)
    return {"ok": True, "config": config}


@settings_new_router.post("/api/settings/wps/files")
async def settings_add_wps_file(request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return _not_logged_in()
    body = await request.json()
    file_id = _extract_file_id(body.get("value", ""))
    if not file_id:
        return JSONResponse({"ok": False, "error": "无法识别文件 ID"}, status_code=400)
    name = str(body.get("name") or file_id).strip()[:100]
    ok, message = db.add_wps_file(uid, file_id, name)
    if not ok:
        return JSONResponse({"ok": False, "error": message}, status_code=400)
    return {"ok": True, "files": db.list_wps_files(uid)}


@settings_new_router.patch("/api/settings/wps/files/{file_id}/default")
async def settings_default_wps_file(file_id: str, request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return _not_logged_in()
    if not any(str(row["file_id"]) == file_id for row in db.list_wps_files(uid)):
        return JSONResponse({"ok": False, "error": "表格不存在"}, status_code=404)
    db.set_default_wps_file(uid, file_id)
    return {"ok": True, "files": db.list_wps_files(uid)}


@settings_new_router.delete("/api/settings/wps/files/{file_id}")
async def settings_delete_wps_file(file_id: str, request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return _not_logged_in()
    db.delete_wps_file(uid, file_id)
    return {"ok": True, "files": db.list_wps_files(uid)}


@settings_new_router.post("/api/settings/wps/account-id")
async def settings_wps_account_id(request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return _not_logged_in()
    body = await request.json()
    account_id = str(body.get("account_id") or "").strip()
    if account_id and not account_id.isdigit():
        return JSONResponse({"ok": False, "error": "account_id 必须为纯数字"}, status_code=400)
    db.save_wps_account_id(uid, account_id)
    return {"ok": True}


@settings_new_router.post("/api/settings/models/{kind}")
async def settings_save_model(kind: str, request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return _not_logged_in()
    body = await request.json()
    provider = str(body.get("provider") or "").strip()
    if provider and provider not in LLM_PRESETS:
        return JSONResponse({"ok": False, "error": "不支持的模型提供商"}, status_code=400)
    values = (
        uid,
        provider,
        str(body.get("api_key") or "").strip(),
        str(body.get("base_url") or "").strip() or None,
        str(body.get("model") or "").strip() or None,
    )
    if kind == "main":
        db.save_llm_key(*values)
    elif kind == "image":
        db.save_image_llm_key(*values)
    else:
        return JSONResponse({"ok": False, "error": "配置类型错误"}, status_code=400)
    return {"ok": True}


@settings_new_router.post("/api/settings/wecom")
async def settings_wecom(request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return _not_logged_in()
    body = await request.json()
    db.set_wecom_userid(uid, str(body.get("wecom_userid") or "").strip())
    return {"ok": True}


@settings_new_router.post("/api/settings/profile")
async def settings_profile(request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return _not_logged_in()
    body = await request.json()
    db.set_display_name(uid, str(body.get("display_name") or "").strip()[:80])
    return {"ok": True}


@settings_new_router.post("/api/settings/feedback")
async def settings_feedback(request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return _not_logged_in()
    body = await request.json()
    content = str(body.get("content") or "").strip()
    fb_type = str(body.get("type") or "suggestion")
    if not content:
        return JSONResponse({"ok": False, "error": "请填写反馈内容"}, status_code=400)
    if fb_type not in {"suggestion", "bug", "other"}:
        fb_type = "other"
    db.add_feedback(uid, content, fb_type)
    return {"ok": True}
