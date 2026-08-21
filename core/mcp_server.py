"""OpenNexus 远程 MCP 服务。

把系统现有智能体工具以 Streamable HTTP MCP 暴露给 WorkBuddy 等客户端。
每个请求都由独立 Bearer token 绑定到唯一系统用户，业务侧继续使用该用户自己的
WPS 授权、微信绑定、记忆、提醒和驾驶舱数据。
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import tempfile
import time
from contextvars import ContextVar
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from auth import db
from auth.wps_oauth import auto_refresh_token_for_user, is_token_expired
from agent import assistant as assistant_module
from core.context_memory import sanitize_memory_content


_identity_var: ContextVar[dict | None] = ContextVar("mcp_identity", default=None)
_MAX_REMOTE_FILE_BYTES = 20 * 1024 * 1024
_SENSITIVE_KEYS = {"token", "authorization", "api_key", "password", "file_base64"}


mcp_server = FastMCP(
    "OpenNexus 部门智能管理助手",
    instructions=(
        "使用当前令牌所属用户的身份操作 OpenNexus。可查询和维护 WPS 多维表格/传统表格，"
        "查询驾驶舱与知识库，生成文档，设置提醒，并通过 WPS、企业微信和个人微信发送消息。"
        "执行删除、批量更新、外发消息等有副作用操作前，应先向用户清楚说明目标。"
    ),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    # 主机名由现有 FastAPI/反向代理决定；Origin 校验在下方中间件中完成，
    # 避免 SDK 仅允许 localhost 的默认值在生产域名下误报 421。
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _identity() -> dict:
    identity = _identity_var.get()
    if not identity:
        raise ToolError("MCP 身份上下文不存在，请重新连接")
    return identity


def _redacted_arguments(arguments: dict) -> str:
    def clean(value: Any, key: str = "") -> Any:
        if key.lower() in _SENSITIVE_KEYS:
            return "***"
        if isinstance(value, dict):
            return {k: clean(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(v) for v in value[:200]]
        if isinstance(value, str) and len(value) > 4000:
            return value[:4000] + "…"
        return value
    return json.dumps(clean(arguments), ensure_ascii=False, default=str)


async def _wps_access_token(user_id: int) -> str:
    token_row = db.get_wps_token(user_id)
    if not token_row or not token_row.get("access_token"):
        raise ToolError("当前用户尚未连接 WPS，请先在 OpenNexus 设置页完成授权")
    expires_at = token_row.get("expires_at") or ""
    if expires_at:
        await auto_refresh_token_for_user(user_id)
        token_row = db.get_wps_token(user_id) or token_row
        if token_row.get("expires_at") and is_token_expired(token_row["expires_at"]):
            raise ToolError("WPS 授权已过期，请在 OpenNexus 中重新连接 WPS")
    return token_row["access_token"]


async def _send_weixin(identity: dict, args: dict) -> dict:
    target_name = (args.get("to_username") or "").strip()
    target = db.get_user_by_username(target_name) or db.get_user_by_display_name(target_name)
    if not target:
        return {"error": f"用户 {target_name} 不存在"}
    target = dict(target)
    weixin_id = db.get_personal_weixin_id(target["id"]) or target.get("weixin_id", "")
    if not weixin_id:
        return {"error": f"用户 {target_name} 尚未绑定个人微信"}

    sender = identity.get("display_name") or identity.get("username") or "OpenNexus"
    text = f"【来自 {sender}】{args.get('text', '')}"
    local_token = db.get_system_config("weixin_bot_token", "")
    try:
        import app as app_module
        candidate_ports = []
        mapped = app_module._wechat_port_map.get(weixin_id)
        if mapped:
            candidate_ports.append(mapped)
        candidate_ports.extend(app_module._wechat_port_map.values())
        candidate_ports.extend(range(3001, 3011))
        candidate_ports = list(dict.fromkeys(candidate_ports))
        async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
            exact_ports = []
            for port in candidate_ports:
                try:
                    response = await client.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                    data = response.json() if response.status_code == 200 else {}
                    if data.get("ok") is True and data.get("userId") == weixin_id:
                        exact_ports.append(port)
                except Exception:
                    continue
            if not exact_ports:
                return {"error": f"用户 {target_name} 的微信桥接未运行或绑定账号不一致"}
            last_error = "微信桥接未返回发送结果"
            for port in exact_ports:
                for retry in range(3):
                    try:
                        response = await client.post(
                            f"http://127.0.0.1:{port}/local/send",
                            json={"to": weixin_id, "text": text, "token": local_token},
                        )
                        try:
                            data = response.json()
                        except Exception:
                            data = {}
                        if response.status_code == 200 and data.get("ok") is True:
                            return {"ok": True, "message": f"已成功发送微信消息给 {target.get('display_name') or target_name}"}
                        last_error = data.get("error") or response.text or "桥接返回空错误"
                        break
                    except Exception as exc:
                        last_error = str(exc)
                        if retry < 2:
                            await asyncio.sleep(1)
            return {"error": f"微信发送失败：{last_error}"}
    except Exception as exc:
        return {"error": f"连接微信桥接失败：{exc}"}


async def _execute_special(name: str, args: dict, identity: dict) -> dict | None:
    user_id = int(identity["user_id"])
    display_name = identity.get("display_name") or identity.get("username") or "OpenNexus"
    if name == "save_memory":
        content, rejected = sanitize_memory_content(args.get("content") or "")
        if not content:
            return {"error": "该内容属于实时 WPS 配置，不写入长期记忆"}
        requested_scope = str(args.get("scope") or "global")
        if requested_scope == "current_table":
            file_id = str(args.get("file_id") or "")
            allowed = {str(item.get("file_id") or "") for item in db.list_wps_files(user_id)}
            if not file_id or file_id not in allowed:
                return {"error": "请为 current_table 记忆提供当前用户已连接的 file_id"}
            scope_type, scope_id, scope_label = "file", file_id, "数据源"
        else:
            # MCP 没有 OpenNexus 对话 ID；current_topic 不冒充全局记忆。
            if requested_scope == "current_topic":
                return {"error": "MCP 调用没有 OpenNexus 当前话题，请使用 global 或 current_table"}
            scope_type, scope_id, scope_label = "global", "", "个人长期"
        db.save_memory_item(
            user_id, content, scope_type=scope_type, scope_id=scope_id,
            category="explicit", source_type="mcp_explicit", confidence=1.0,
        )
        if scope_type == "global":
            old = db.get_user_memory(user_id)
            db.save_user_memory(user_id, (old + "\n- " + content) if old else "- " + content)
        note = "；实时配置已忽略" if rejected else ""
        return {"ok": True, "message": f"已保存为{scope_label}记忆：{content}{note}"}
    if name == "send_notification":
        return assistant_module._send_notification(args, sender_name=display_name)
    if name == "get_change_log":
        return assistant_module._get_change_log(args.get("file_id"), args.get("limit", 20), uid=user_id)
    if name == "send_wecom_message":
        webhook_url = db.get_system_config("wecom_webhook_url")
        if not webhook_url:
            return {"error": "企业微信机器人尚未配置"}
        wecom_uid = db.get_wecom_userid(args.get("to_username", ""))
        return await assistant_module._wecom_send(webhook_url, args["text"], wecom_uid)
    if name == "send_weixin_message":
        return await _send_weixin(identity, args)
    if name == "add_reminder":
        content = (args.get("content") or "").strip()
        remind_at = (args.get("remind_at") or "").strip()
        event_at = (args.get("event_at") or remind_at).strip()
        try:
            remind_dt = datetime.strptime(remind_at, "%Y-%m-%d %H:%M")
            datetime.strptime(event_at, "%Y-%m-%d %H:%M")
        except ValueError as exc:
            return {"error": "remind_at 和 event_at 必须使用 YYYY-MM-DD HH:MM 格式"}
        if remind_dt <= db.beijing_now():
            return {"error": f"提醒时间 {remind_at} 已经过期"}
        reminder_id = db.add_reminder(user_id, content, remind_at, event_at=event_at)
        return {"ok": True, "reminder_id": reminder_id, "message": f"提醒已设置，将在 {remind_at} 推送；事件时间 {event_at}"}
    if name == "list_reminders":
        rows = db.list_reminders(user_id)
        return {"ok": True, "reminders": rows, "message": f"共 {len(rows)} 条待触发提醒"}
    if name == "cancel_reminder":
        reminder_id = int(args.get("reminder_id"))
        if db.cancel_reminder(reminder_id, user_id):
            return {"ok": True, "message": f"提醒 {reminder_id} 已取消"}
        return {"error": f"提醒 {reminder_id} 不存在或无权取消"}
    return None


async def execute_tool(name: str, arguments: dict) -> dict:
    identity = _identity()
    started = time.perf_counter()
    success = False
    error_text = ""
    try:
        special = await _execute_special(name, arguments, identity)
        if special is not None:
            result = special
        elif name == "get_current_user":
            result = {
                "ok": True,
                "user": {
                    "id": identity["user_id"],
                    "username": identity.get("username", ""),
                    "display_name": identity.get("display_name", ""),
                    "is_admin": bool(identity.get("is_admin")),
                },
            }
        elif name == "list_wps_files":
            files = db.list_wps_files(identity["user_id"])
            default = db.get_default_wps_file(identity["user_id"])
            result = {"ok": True, "default_file_id": (default or {}).get("file_id", ""), "files": files}
        elif name == "get_dashboard":
            from core.dashboard_service import generate_dashboard
            file_id = arguments.get("file_id") or (db.get_default_wps_file(identity["user_id"]) or {}).get("file_id", "")
            if not file_id:
                result = {"error": "尚未配置 WPS 多维表格"}
            else:
                payload = await generate_dashboard(
                    identity["user_id"], file_id, arguments.get("view", "overview"),
                    arguments.get("date") or date.today().isoformat(),
                    force=bool(arguments.get("refresh", False) or arguments.get("ai_summary", False)),
                    use_ai=bool(arguments.get("ai_summary", False)),
                )
                result = {"ok": True, "data": payload}
        elif name == "list_dashboard_dates":
            file_id = arguments.get("file_id") or (db.get_default_wps_file(identity["user_id"]) or {}).get("file_id", "")
            view = arguments.get("view", "daily")
            result = {"ok": True, "dates": db.list_dashboard_snapshot_dates(identity["user_id"], file_id, view)}
        elif name == "get_mcp_audit_log":
            result = {"ok": True, "logs": db.list_mcp_audit_log(identity["user_id"], arguments.get("limit", 100))}
        elif name == "upload_attachment_base64":
            encoded = arguments.get("file_base64") or ""
            try:
                content = base64.b64decode(encoded, validate=True)
            except Exception:
                result = {"error": "file_base64 不是有效的 Base64 数据"}
            else:
                if len(content) > _MAX_REMOTE_FILE_BYTES:
                    result = {"error": "远程附件最大为 20MB"}
                else:
                    token = await _wps_access_token(identity["user_id"])
                    safe_name = Path(arguments.get("file_name") or "attachment.bin").name
                    suffix = Path(safe_name).suffix
                    temp_path = ""
                    try:
                        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
                            temp_file.write(content)
                            temp_path = temp_file.name
                        result = await assistant_module._upload_and_attach(
                            token, arguments["file_id"], arguments["sheet_id"], arguments["record_id"],
                            arguments["field_name"], temp_path, safe_name,
                        )
                    finally:
                        if temp_path:
                            Path(temp_path).unlink(missing_ok=True)
        elif name == "upload_and_attach":
            allowed_roots = [Path(tempfile.gettempdir()).resolve(), (Path(__file__).parent.parent / "data" / "uploads").resolve()]
            requested = Path(arguments.get("file_path") or "").resolve()
            if not any(requested.is_relative_to(root) for root in allowed_roots):
                result = {"error": "远程 MCP 不允许读取任意服务器路径，请使用 upload_attachment_base64"}
            else:
                token = await _wps_access_token(identity["user_id"])
                call = assistant_module.TOOL_MAP[name](token, arguments)
                result = await call if inspect.isawaitable(call) else call
        elif name in assistant_module.TOOL_MAP:
            local_without_wps = {"search_knowledge", "list_system_users", "send_wps_bot_message"}
            token = "" if name in local_without_wps else await _wps_access_token(identity["user_id"])
            call = assistant_module.TOOL_MAP[name](token, arguments)
            result = await call if inspect.isawaitable(call) else call
        else:
            result = {"error": f"未知 MCP 工具：{name}"}

        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        if result.get("error"):
            raise ToolError(str(result["error"]))
        success = True
        return result
    except Exception as exc:
        error_text = str(exc)
        if isinstance(exc, ToolError):
            raise
        raise ToolError(error_text) from exc
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000)
        db.add_mcp_audit_log(
            identity["user_id"], identity.get("id"), name,
            _redacted_arguments(arguments), success, error_text, duration_ms,
        )


def _annotation(schema: dict) -> Any:
    kind = schema.get("type")
    return {"string": str, "integer": int, "number": float, "boolean": bool,
            "array": list[Any], "object": dict[str, Any]}.get(kind, Any)


def _register_tool(spec: dict) -> None:
    function = spec["function"]
    name = function["name"]
    schema = function.get("parameters") or {"type": "object", "properties": {}}
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])

    async def dynamic_tool(**kwargs: Any) -> dict:
        return await execute_tool(name, {key: value for key, value in kwargs.items() if value is not None})

    dynamic_tool.__name__ = name
    dynamic_tool.__doc__ = function.get("description", "")
    dynamic_tool.__signature__ = inspect.Signature(
        [
            inspect.Parameter(
                key,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=_annotation(value),
                default=inspect.Parameter.empty if key in required else None,
            )
            for key, value in properties.items()
        ],
        return_annotation=dict,
    )
    mcp_server.add_tool(dynamic_tool, name=name, description=function.get("description", name))
    tool = mcp_server._tool_manager.get_tool(name)
    if tool:
        tool.parameters = schema


_EXTRA_TOOLS = [
    {"type": "function", "function": {"name": "get_current_user", "description": "返回当前 MCP 令牌绑定的 OpenNexus 用户身份。", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "list_wps_files", "description": "列出当前用户在 OpenNexus 中配置的 WPS 文件及默认文件。", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "get_dashboard", "description": "查询整体、每日进展、任务或项目驾驶舱；默认读取本地高速缓存。", "parameters": {"type": "object", "properties": {"view": {"type": "string", "enum": ["overview", "daily", "tasks", "projects"], "description": "驾驶舱页面"}, "date": {"type": "string", "description": "YYYY-MM-DD"}, "file_id": {"type": "string", "description": "不填使用默认 WPS 文件"}, "refresh": {"type": "boolean", "description": "是否先从 WPS 刷新数据"}, "ai_summary": {"type": "boolean", "description": "是否调用用户配置的大模型重写分析"}}, "required": []}}},
    {"type": "function", "function": {"name": "list_dashboard_dates", "description": "列出已有的驾驶舱历史快照日期。", "parameters": {"type": "object", "properties": {"file_id": {"type": "string"}, "view": {"type": "string", "enum": ["overview", "daily", "tasks", "projects"]}}, "required": []}}},
    {"type": "function", "function": {"name": "get_mcp_audit_log", "description": "查看当前用户最近的 MCP 工具调用审计结果。", "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "1-500，默认100"}}, "required": []}}},
    {"type": "function", "function": {"name": "upload_attachment_base64", "description": "从远程客户端上传文件并写入 WPS 多维表格记录附件字段，最大20MB。", "parameters": {"type": "object", "properties": {"file_id": {"type": "string"}, "sheet_id": {"type": "integer"}, "record_id": {"type": "string"}, "field_name": {"type": "string"}, "file_name": {"type": "string"}, "file_base64": {"type": "string", "description": "文件内容的标准 Base64"}}, "required": ["file_id", "sheet_id", "record_id", "field_name", "file_name", "file_base64"]}}},
]

for _tool_spec in [*assistant_module.TOOLS, *_EXTRA_TOOLS]:
    _register_tool(_tool_spec)


class MCPBearerMiddleware:
    """为 MCP 子应用绑定用户，并做基本 Origin 防护。"""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])}
        origin = headers.get("origin", "")
        host = headers.get("host", "")
        allowed_origins = {item.strip() for item in os.getenv("MCP_ALLOWED_ORIGINS", "").split(",") if item.strip()}
        if origin.startswith(("http://", "https://")) and origin not in allowed_origins:
            origin_host = origin.split("//", 1)[-1].rstrip("/")
            if origin_host != host:
                response = JSONResponse({"error": "MCP Origin 不受信任"}, status_code=403)
                await response(scope, receive, send)
                return
        auth = headers.get("authorization", "")
        raw_token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        identity = db.verify_mcp_token(raw_token)
        if not identity:
            response = JSONResponse(
                {"error": "无效、过期或已撤销的 MCP Bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        identity["user_id"] = identity["user_id"]
        marker = _identity_var.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            _identity_var.reset(marker)


mcp_http_app = MCPBearerMiddleware(mcp_server.streamable_http_app())
