"""独立 HTML 设置页及其 JSON API。

所有状态都通过 Starlette Session 确定当前用户。
"""
from __future__ import annotations

import re
import hashlib
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from agent.assistant import LLM_PRESETS
from auth import db
from core.tool_governance import scope_options


settings_new_router = APIRouter()
_HTML_FILE = Path(__file__).resolve().parent.parent / "static" / "settings_new.html"
_CUSTOM_PROVIDER = "custom_openai"
_CUSTOM_PROVIDER_PREFIX = _CUSTOM_PROVIDER + ":"
_REASONING_MODES = {"auto", "on", "off"}
_REASONING_EFFORTS = {"auto", "low", "medium", "high", "max"}


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


def _advanced_config(body: dict, kind: str) -> tuple[dict | None, str | None]:
    raw = body.get("advanced") or {}
    if not isinstance(raw, dict):
        return None, "高级配置格式错误"

    mode = str(raw.get("reasoning_mode") or "auto").lower()
    effort = str(raw.get("reasoning_effort") or "auto").lower()
    if mode not in _REASONING_MODES:
        return None, "推理模式无效"
    if effort not in _REASONING_EFFORTS:
        return None, "思考强度无效"

    def optional_int(name: str, minimum: int, maximum: int):
        value = raw.get(name)
        if value in (None, "", 0, "0"):
            return None, None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None, f"{name} 必须为整数"
        if not minimum <= number <= maximum:
            return None, f"{name} 必须在 {minimum}～{maximum} 之间"
        return number, None

    context_window, error = optional_int("context_window", 1024, 2_000_000)
    if error:
        return None, error
    max_output_tokens, error = optional_int("max_output_tokens", 128, 262_144)
    if error:
        return None, error

    return {
        "supports_tools": bool(raw.get("supports_tools", kind == "main")),
        "supports_vision": bool(raw.get("supports_vision", kind == "image")),
        "reasoning_mode": mode,
        "reasoning_effort": effort,
        "context_window": context_window,
        "max_output_tokens": max_output_tokens,
    }, None


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
    result[_CUSTOM_PROVIDER] = {
        "name": "自定义 OpenAI 兼容接口",
        "base_url": "",
        "model": "",
        "models": [],
        "custom": True,
    }
    return result


def _is_custom_provider(provider: str) -> bool:
    return provider == _CUSTOM_PROVIDER or provider.startswith(_CUSTOM_PROVIDER_PREFIX)


def _custom_provider_id(base_url: str, model: str) -> str:
    digest = hashlib.sha256(f"{base_url}\n{model}".encode("utf-8")).hexdigest()[:16]
    return _CUSTOM_PROVIDER_PREFIX + digest


def _model_profiles(uid: int, kind: str):
    rows = db.list_custom_provider_configs(uid, image=(kind == "image"))
    result = []
    for row in rows:
        model = str(row.get("model") or "自定义模型")
        base_url = str(row.get("base_url") or "")
        host = re.sub(r"^https?://", "", base_url, flags=re.IGNORECASE).split("/", 1)[0]
        result.append({
            "id": row["provider"],
            "name": f"自定义 · {model}" + (f"（{host}）" if host else ""),
            "base_url": base_url,
            "model": model,
        })
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
        "model_profiles": {
            "main": _model_profiles(uid, "main"),
            "image": _model_profiles(uid, "image"),
        },
        "personal_weixin_id": db.get_personal_weixin_id(uid) or "",
        "mcp": {
            "endpoint": str(request.base_url).rstrip("/") + "/mcp/",
            "tokens": db.list_mcp_tokens(uid),
            "scope_options": scope_options(),
            "approvals": db.list_mcp_tool_approvals(uid),
        },
    }


@settings_new_router.get("/api/settings/mcp/approvals")
async def settings_mcp_approvals(request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return _not_logged_in()
    return {"ok": True, "approvals": db.list_mcp_tool_approvals(uid)}


@settings_new_router.post("/api/settings/mcp/approvals/{approval_id}/decision")
async def settings_mcp_approval_decision(approval_id: str, request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return _not_logged_in()
    body = await request.json()
    decision = str(body.get("decision") or "").strip().lower()
    if decision not in {"approved", "rejected"}:
        return JSONResponse({"ok": False, "error": "审批结果无效"}, status_code=400)
    row = db.decide_mcp_tool_approval(uid, approval_id, decision)
    if not row:
        return JSONResponse(
            {"ok": False, "error": "审批单不存在、已处理或已过期"},
            status_code=409,
        )
    return {"ok": True, "approval": row, "approvals": db.list_mcp_tool_approvals(uid)}


@settings_new_router.get("/api/settings/provider/{kind}/{provider}")
async def settings_provider(kind: str, provider: str, request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return _not_logged_in()
    if provider not in LLM_PRESETS and not _is_custom_provider(provider):
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
    if provider and provider not in LLM_PRESETS and not _is_custom_provider(provider):
        return JSONResponse({"ok": False, "error": "不支持的模型提供商"}, status_code=400)
    api_key = str(body.get("api_key") or "").strip()
    base_url = str(body.get("base_url") or "").strip()
    model = str(body.get("model") or "").strip()
    advanced, advanced_error = _advanced_config(body, kind)
    if advanced_error:
        return JSONResponse({"ok": False, "error": advanced_error}, status_code=400)
    if _is_custom_provider(provider):
        # 用户常从服务商文档复制完整请求地址；AsyncOpenAI 需要的是 API 根地址。
        base_url = re.sub(r"/chat/completions/?$", "", base_url, flags=re.IGNORECASE)
        if not re.fullmatch(r"https?://[^\s]+", base_url, flags=re.IGNORECASE):
            return JSONResponse(
                {"ok": False, "error": "自定义接口必须填写有效的 http(s) Base URL"},
                status_code=400,
            )
        if not model:
            return JSONResponse({"ok": False, "error": "自定义接口必须填写模型 ID"}, status_code=400)
        if not api_key:
            return JSONResponse({"ok": False, "error": "自定义接口必须填写 API Key"}, status_code=400)
        if provider == _CUSTOM_PROVIDER:
            provider = _custom_provider_id(base_url, model)
    values = (
        uid,
        provider,
        api_key,
        base_url or None,
        model or None,
    )
    if kind == "main":
        db.save_llm_key(*values, advanced)
    elif kind == "image":
        db.save_image_llm_key(*values, advanced)
    else:
        return JSONResponse({"ok": False, "error": "配置类型错误"}, status_code=400)
    return {"ok": True, "provider_id": provider}


@settings_new_router.delete("/api/settings/provider/{kind}/{provider}")
async def settings_delete_provider(kind: str, provider: str, request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return _not_logged_in()
    if kind not in {"main", "image"} or not provider.startswith(_CUSTOM_PROVIDER_PREFIX):
        return JSONResponse({"ok": False, "error": "只能删除自定义模型配置"}, status_code=400)
    current = db.get_llm_key(uid) if kind == "main" else db.get_image_llm_key(uid)
    if current and current.get("provider") == provider:
        return JSONResponse(
            {"ok": False, "error": "该模型正在使用，请先切换并保存其他模型后再删除"},
            status_code=409,
        )
    db.delete_custom_provider_config(uid, provider, image=(kind == "image"))
    return {"ok": True, "profiles": _model_profiles(uid, kind)}


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
