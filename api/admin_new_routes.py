"""独立 HTML 管理后台及其 JSON API。"""
from __future__ import annotations

import secrets
import string
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from auth import db


admin_new_router = APIRouter()
_HTML_FILE = Path(__file__).resolve().parent.parent / "static" / "admin_new.html"


def _admin(request: Request):
    uid = request.session.get("uid")
    user = db.get_user_by_id(int(uid)) if uid else None
    if not user or not user["is_enabled"] or not user["is_admin"]:
        return None, None
    return int(uid), dict(user)


def _denied(status: int = 403):
    return JSONResponse({"ok": False, "error": "无权访问管理后台"}, status_code=status)


def _knowledge_rows():
    chunks = db.get_chunk_counts()
    return [{
        "id": row["id"], "title": row["title"],
        "file_name": row.get("file_name") or "",
        "category": row.get("category") or "规章制度",
        "is_enabled": bool(row.get("is_enabled")),
        "created_at": row.get("created_at") or "",
        "chars": len(row.get("content") or ""),
        "chunks": chunks.get(row["id"], 0),
    } for row in db.list_knowledge(enabled_only=False)]


@admin_new_router.get("/admin", response_class=HTMLResponse)
@admin_new_router.get("/admin-new", response_class=HTMLResponse)
async def admin_new_page(request: Request):
    uid, user = _admin(request)
    if not uid:
        if request.session.get("uid"):
            return RedirectResponse("/", status_code=302)
        return RedirectResponse("/login?next=" + request.url.path, status_code=302)
    return HTMLResponse(_HTML_FILE.read_text(encoding="utf-8"))


@admin_new_router.get("/api/admin-new/bootstrap")
async def admin_bootstrap(request: Request):
    uid, admin = _admin(request)
    if not uid:
        return _denied()
    users = db.list_users()
    feedback = db.list_feedback()
    knowledge = _knowledge_rows()
    embed = db.get_embed_config() or {}
    return {
        "ok": True,
        "admin": {"id": uid, "username": admin.get("username", "")},
        "stats": {
            "users": len(users),
            "enabled": sum(bool(x.get("is_enabled")) for x in users),
            "admins": sum(bool(x.get("is_admin")) for x in users),
            "pending_feedback": sum(x.get("status") == "pending" for x in feedback),
        },
        "users": users,
        "feedback": feedback,
        "knowledge": knowledge,
        "logs": db.get_change_log(limit=200),
        "config": {
            "wecom_webhook_url": db.get_system_config("wecom_webhook_url", ""),
            "wps_app_id": db.get_system_config("wps_app_id", ""),
            "wps_app_secret_configured": bool(db.get_system_config("wps_app_secret", "")),
            "embed_base_url": embed.get("base_url", "https://api.siliconflow.cn/v1"),
            "embed_model": embed.get("model", "BAAI/bge-m3"),
            "embed_key_configured": bool(embed.get("api_key")),
        },
    }


@admin_new_router.patch("/api/admin-new/users/{user_id}")
async def admin_update_user(user_id: int, request: Request):
    uid, _ = _admin(request)
    if not uid:
        return _denied()
    body = await request.json()
    role = str(body.get("role") or "staff")
    if role not in {"staff", "manager", "executive", "admin"}:
        return JSONResponse({"ok": False, "error": "角色无效"}, status_code=400)
    db.set_display_name(user_id, str(body.get("display_name") or "").strip()[:80])
    db.set_user_role(user_id, role)
    db.set_user_department(user_id, str(body.get("department") or "").strip()[:100])
    if role == "admin":
        db.set_user_admin(user_id, True)
    return {"ok": True}


@admin_new_router.patch("/api/admin-new/users/{user_id}/admin")
async def admin_toggle_admin(user_id: int, request: Request):
    uid, _ = _admin(request)
    if not uid:
        return _denied()
    body = await request.json()
    enabled = bool(body.get("enabled"))
    if user_id == uid and not enabled:
        return JSONResponse({"ok": False, "error": "不能撤销自己的管理员权限"}, status_code=400)
    db.set_user_admin(user_id, enabled)
    return {"ok": True}


@admin_new_router.patch("/api/admin-new/users/{user_id}/enabled")
async def admin_toggle_user(user_id: int, request: Request):
    uid, _ = _admin(request)
    if not uid:
        return _denied()
    body = await request.json()
    enabled = bool(body.get("enabled"))
    if user_id == uid and not enabled:
        return JSONResponse({"ok": False, "error": "不能禁用当前登录账号"}, status_code=400)
    db.set_user_enabled(user_id, enabled)
    return {"ok": True}


@admin_new_router.post("/api/admin-new/users/{user_id}/reset-password")
async def admin_reset_password(user_id: int, request: Request):
    uid, _ = _admin(request)
    if not uid:
        return _denied()
    alphabet = string.ascii_letters + string.digits
    password = "".join(secrets.choice(alphabet) for _ in range(12))
    db.reset_password(user_id, password)
    return {"ok": True, "temporary_password": password}


@admin_new_router.delete("/api/admin-new/users/{user_id}")
async def admin_delete_user(user_id: int, request: Request):
    uid, _ = _admin(request)
    if not uid:
        return _denied()
    if user_id == uid:
        return JSONResponse({"ok": False, "error": "不能删除当前登录账号"}, status_code=400)
    db.delete_user(user_id)
    return {"ok": True}


@admin_new_router.patch("/api/admin-new/feedback/{feedback_id}")
async def admin_feedback(feedback_id: int, request: Request):
    uid, _ = _admin(request)
    if not uid:
        return _denied()
    body = await request.json()
    status = str(body.get("status") or "done")
    if status not in {"pending", "done"}:
        return JSONResponse({"ok": False, "error": "状态无效"}, status_code=400)
    db.update_feedback_status(feedback_id, status)
    return {"ok": True}


@admin_new_router.patch("/api/admin-new/knowledge/{knowledge_id}")
async def admin_knowledge_toggle(knowledge_id: int, request: Request):
    uid, _ = _admin(request)
    if not uid:
        return _denied()
    body = await request.json()
    db.toggle_knowledge(knowledge_id, bool(body.get("enabled")))
    return {"ok": True}


@admin_new_router.delete("/api/admin-new/knowledge/{knowledge_id}")
async def admin_knowledge_delete(knowledge_id: int, request: Request):
    uid, _ = _admin(request)
    if not uid:
        return _denied()
    db.delete_knowledge(knowledge_id)
    return {"ok": True}


@admin_new_router.post("/api/admin-new/knowledge/bulk-delete")
async def admin_knowledge_bulk_delete(request: Request):
    uid, _ = _admin(request)
    if not uid:
        return _denied()
    body = await request.json()
    ids = [int(item) for item in body.get("ids", []) if str(item).isdigit()]
    return {"ok": True, "deleted": db.bulk_delete_knowledge(ids)}


@admin_new_router.post("/api/admin-new/config")
async def admin_save_config(request: Request):
    uid, _ = _admin(request)
    if not uid:
        return _denied()
    body = await request.json()
    section = str(body.get("section") or "")
    if section == "wecom":
        db.set_system_config("wecom_webhook_url", str(body.get("webhook_url") or "").strip())
    elif section == "wps":
        db.set_system_config("wps_app_id", str(body.get("app_id") or "").strip())
        secret = str(body.get("app_secret") or "").strip()
        if secret:
            db.set_system_config("wps_app_secret", secret)
    elif section == "embed":
        key = str(body.get("api_key") or "").strip()
        old = db.get_embed_config() or {}
        if not key:
            key = old.get("api_key", "")
        if not key:
            return JSONResponse({"ok": False, "error": "请填写嵌入模型 API Key"}, status_code=400)
        db.save_embed_config(key, str(body.get("base_url") or "").strip(), str(body.get("model") or "").strip())
    else:
        return JSONResponse({"ok": False, "error": "配置类型无效"}, status_code=400)
    return {"ok": True}
