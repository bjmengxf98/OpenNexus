"""MCP 工具的代码级权限、风险与审批 Hook。

提示词只能提醒模型谨慎；本模块在真正执行业务工具之前再次校验令牌作用域，
并把高风险调用暂停为一次性审批单。审批单与用户、令牌、工具名和参数摘要绑定。
"""
from __future__ import annotations

import hashlib
import inspect
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from auth import db


APPROVAL_ARGUMENT = "approval_id"
_SENSITIVE_KEYS = {
    "token", "authorization", "api_key", "password", "file_base64",
    "access_token", "refresh_token", "webhook_url",
}

MCP_SCOPE_DEFINITIONS = (
    {"id": "read", "label": "读取业务数据", "description": "查询 WPS、驾驶舱、知识库和审计记录"},
    {"id": "write_records", "label": "写入业务数据", "description": "新增或更新记录、单元格和关联关系"},
    {"id": "manage_structure", "label": "管理表格结构", "description": "工作表、字段、视图、表单和 webhook"},
    {"id": "send_messages", "label": "发送外部消息", "description": "邮件、WPS、企业微信和个人微信"},
    {"id": "manage_memory", "label": "管理长期记忆", "description": "写入当前用户的长期记忆"},
    {"id": "manage_reminders", "label": "管理提醒", "description": "查看、新增和取消提醒"},
    {"id": "upload_files", "label": "上传附件", "description": "向 WPS 记录上传附件"},
    {"id": "generate_documents", "label": "生成文档", "description": "生成并保存业务文档"},
)
_VALID_SCOPES = {item["id"] for item in MCP_SCOPE_DEFINITIONS}

_READ_TOOLS = {
    "get_current_user", "list_wps_files", "get_schema", "list_records",
    "list_dashboards", "get_form_meta", "list_form_fields", "get_parent_status",
    "list_children", "list_hooks", "search_knowledge", "get_change_log",
    "list_wps_contacts", "list_system_users", "sheets_list_worksheets",
    "sheets_get_range", "sheets_find_range", "get_dashboard",
    "list_dashboard_dates", "get_mcp_audit_log",
}
_WRITE_RECORD_TOOLS = {
    "create_records", "update_records", "delete_records", "bind_children",
    "unbind_children", "sheets_update_range", "sheets_delete_range",
}
_STRUCTURE_TOOLS = {
    "create_sheet", "delete_sheet", "create_fields", "update_fields",
    "delete_fields", "create_view", "delete_view", "copy_dashboard",
    "update_form_meta", "update_form_field", "enable_parent", "disable_parent",
    "create_hook", "delete_hook", "sheets_create_file", "sheets_create_worksheet",
    "sheets_update_worksheet", "sheets_delete_worksheets", "sheets_copy_worksheet",
}
_MESSAGE_TOOLS = {
    "send_notification", "send_wps_bot_message", "send_wecom_message",
    "send_weixin_message",
}
_REMINDER_TOOLS = {"add_reminder", "list_reminders", "cancel_reminder"}
_UPLOAD_TOOLS = {"upload_and_attach", "upload_attachment_base64"}
_DOCUMENT_TOOLS = {"generate_document"}

_DESTRUCTIVE_TOOLS = {
    "delete_records", "delete_sheet", "delete_fields", "delete_view",
    "disable_parent", "unbind_children", "delete_hook",
    "sheets_delete_worksheets", "sheets_delete_range",
}
_STRUCTURE_APPROVAL_TOOLS = set(_STRUCTURE_TOOLS)
_ALWAYS_APPROVAL_TOOLS = _DESTRUCTIVE_TOOLS | _STRUCTURE_APPROVAL_TOOLS | _MESSAGE_TOOLS
_BATCH_ARGUMENTS = {
    "create_records": "records",
    "update_records": "records",
    "sheets_update_range": "range_data",
}

_TOOL_LABELS = {
    "create_records": "新增记录", "update_records": "更新记录", "delete_records": "删除记录",
    "create_sheet": "新建工作表", "delete_sheet": "删除工作表",
    "create_fields": "新建字段", "update_fields": "更新字段", "delete_fields": "删除字段",
    "create_view": "新建视图", "delete_view": "删除视图", "copy_dashboard": "复制仪表盘",
    "update_form_meta": "更新表单", "update_form_field": "更新表单字段",
    "enable_parent": "启用父子关系", "disable_parent": "禁用父子关系",
    "bind_children": "绑定子记录", "unbind_children": "解绑子记录",
    "create_hook": "创建 webhook", "delete_hook": "删除 webhook",
    "send_notification": "发送邮件", "send_wps_bot_message": "发送 WPS 消息",
    "send_wecom_message": "发送企业微信", "send_weixin_message": "发送个人微信",
    "sheets_create_worksheet": "新建工作表标签页",
    "sheets_update_worksheet": "调整工作表标签页",
    "sheets_delete_worksheets": "删除工作表标签页",
    "sheets_copy_worksheet": "复制工作表标签页",
    "sheets_update_range": "更新单元格", "sheets_delete_range": "删除单元格区域",
}


@dataclass(frozen=True)
class ToolPolicy:
    required_scope: str
    risk: str
    approval_required: bool
    read_only: bool
    destructive: bool
    idempotent: bool
    open_world: bool


@dataclass
class ToolDecision:
    action: str = "allow"  # allow / ask / deny
    message: str = ""
    payload: dict[str, Any] | None = None


@dataclass
class ToolHookContext:
    identity: dict[str, Any]
    tool_name: str
    arguments: dict[str, Any]
    policy: ToolPolicy
    started_at: float = field(default_factory=time.perf_counter)
    decision: ToolDecision = field(default_factory=ToolDecision)
    approval_id: str = ""


PreToolHook = Callable[[ToolHookContext], ToolDecision | None | Awaitable[ToolDecision | None]]
PostToolHook = Callable[[ToolHookContext, dict[str, Any] | None, str], None | Awaitable[None]]
_pre_tool_hooks: list[PreToolHook] = []
_post_tool_hooks: list[PostToolHook] = []


def scope_options() -> list[dict[str, str]]:
    return [dict(item) for item in MCP_SCOPE_DEFINITIONS]


def normalize_requested_scopes(value: Any) -> list[str]:
    """校验创建令牌时提交的 scopes；缺省保持旧版的 all 行为。"""
    if value is None:
        return ["all"]
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, (list, tuple, set)):
        raise ValueError("令牌权限格式错误")
    scopes = list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
    if not scopes:
        raise ValueError("请至少选择一项令牌权限")
    invalid = sorted(set(scopes) - _VALID_SCOPES - {"all"})
    if invalid:
        raise ValueError("未知令牌权限：" + "、".join(invalid))
    if "all" in scopes:
        return ["all"]
    return scopes


def granted_scopes(value: Any) -> set[str]:
    if value is None:
        return set()
    try:
        return set(normalize_requested_scopes(value))
    except ValueError:
        return set()


def _required_scope(tool_name: str) -> str:
    if tool_name in _READ_TOOLS:
        return "read"
    if tool_name in _WRITE_RECORD_TOOLS:
        return "write_records"
    if tool_name in _STRUCTURE_TOOLS:
        return "manage_structure"
    if tool_name in _MESSAGE_TOOLS:
        return "send_messages"
    if tool_name == "save_memory":
        return "manage_memory"
    if tool_name in _REMINDER_TOOLS:
        return "manage_reminders"
    if tool_name in _UPLOAD_TOOLS:
        return "upload_files"
    if tool_name in _DOCUMENT_TOOLS:
        return "generate_documents"
    # 新增工具在完成显式分级前按结构变更处理，避免意外落入只读权限。
    return "manage_structure"


def policy_for_tool(tool_name: str, arguments: dict[str, Any] | None = None) -> ToolPolicy:
    arguments = arguments or {}
    required_scope = _required_scope(tool_name)
    read_only = required_scope == "read" or tool_name == "list_reminders"
    destructive = tool_name in _DESTRUCTIVE_TOOLS
    approval_required = tool_name in _ALWAYS_APPROVAL_TOOLS
    batch_key = _BATCH_ARGUMENTS.get(tool_name)
    if batch_key and isinstance(arguments.get(batch_key), list) and len(arguments[batch_key]) > 1:
        approval_required = True
    if tool_name in _MESSAGE_TOOLS:
        risk = "external_message"
    elif destructive:
        risk = "destructive"
    elif tool_name in _STRUCTURE_TOOLS:
        risk = "structure_change"
    elif approval_required:
        risk = "batch_write"
    elif read_only:
        risk = "read"
    else:
        risk = "write"
    return ToolPolicy(
        required_scope=required_scope,
        risk=risk,
        approval_required=approval_required,
        read_only=read_only,
        destructive=destructive,
        idempotent=read_only,
        open_world=tool_name in _MESSAGE_TOOLS,
    )


def potentially_requires_approval(tool_name: str) -> bool:
    return tool_name in _ALWAYS_APPROVAL_TOOLS or tool_name in _BATCH_ARGUMENTS or tool_name not in (
        _READ_TOOLS | _WRITE_RECORD_TOOLS | _STRUCTURE_TOOLS | _MESSAGE_TOOLS |
        _REMINDER_TOOLS | _UPLOAD_TOOLS | _DOCUMENT_TOOLS | {"save_memory"}
    )


def business_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in arguments.items() if key != APPROVAL_ARGUMENT}


def arguments_hash(arguments: dict[str, Any]) -> str:
    canonical = json.dumps(business_arguments(arguments), ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def redacted_arguments(arguments: dict[str, Any]) -> str:
    def clean(value: Any, key: str = "") -> Any:
        if key.lower() in _SENSITIVE_KEYS:
            return "***"
        if isinstance(value, dict):
            return {str(k): clean(v, str(k)) for k, v in value.items() if k != APPROVAL_ARGUMENT}
        if isinstance(value, list):
            return [clean(item) for item in value[:200]]
        if isinstance(value, str) and len(value) > 4000:
            return value[:4000] + "…"
        return value
    return json.dumps(clean(arguments), ensure_ascii=False, sort_keys=True, default=str)


def approval_summary(tool_name: str, arguments: dict[str, Any]) -> str:
    label = _TOOL_LABELS.get(tool_name, tool_name)
    parts: list[str] = []
    for key, title in (("file_id", "文件"), ("sheet_id", "工作表"),
                       ("worksheet_id", "标签页"), ("view_id", "视图"),
                       ("to_username", "收件人"), ("to_name", "收件人"),
                       ("to_email", "邮箱"), ("subject", "主题"), ("name", "名称")):
        value = arguments.get(key)
        if value not in (None, ""):
            parts.append(f"{title}={str(value)[:120]}")
    for key, title in (("records", "记录"), ("record_ids", "记录"),
                       ("fields", "字段"), ("field_ids", "字段"),
                       ("worksheet_ids", "标签页"), ("range_data", "区域")):
        value = arguments.get(key)
        if isinstance(value, list):
            parts.append(f"{title}{len(value)}项")
    return label + ("（" + "，".join(parts) + "）" if parts else "")


def register_pre_tool_hook(hook: PreToolHook) -> None:
    _pre_tool_hooks.append(hook)


def register_post_tool_hook(hook: PostToolHook) -> None:
    _post_tool_hooks.append(hook)


async def _invoke_hook(hook: Callable[..., Any], *args: Any) -> Any:
    result = hook(*args)
    return await result if inspect.isawaitable(result) else result


def new_tool_context(identity: dict[str, Any], tool_name: str,
                     arguments: dict[str, Any]) -> ToolHookContext:
    clean_arguments = business_arguments(arguments)
    return ToolHookContext(
        identity=identity,
        tool_name=tool_name,
        arguments=clean_arguments,
        policy=policy_for_tool(tool_name, clean_arguments),
        approval_id=str(arguments.get(APPROVAL_ARGUMENT) or "").strip(),
    )


async def run_pre_tool_hooks(context: ToolHookContext) -> ToolDecision:
    for hook in _pre_tool_hooks:
        decision = await _invoke_hook(hook, context)
        if decision and decision.action != "allow":
            context.decision = decision
            return decision
    context.decision = ToolDecision()
    return context.decision


async def run_post_tool_hooks(context: ToolHookContext, result: dict[str, Any] | None,
                              error: str = "") -> None:
    for hook in _post_tool_hooks:
        try:
            await _invoke_hook(hook, context, result, error)
        except Exception:
            # 审计或扩展 Hook 不能覆盖真实工具结果。
            continue


def _scope_hook(context: ToolHookContext) -> ToolDecision | None:
    scopes = granted_scopes(context.identity.get("scopes"))
    required = context.policy.required_scope
    if "all" in scopes or required in scopes:
        return None
    label = next((item["label"] for item in MCP_SCOPE_DEFINITIONS if item["id"] == required), required)
    return ToolDecision(
        action="deny",
        message=f"当前 MCP 令牌缺少权限：{label}（{required}）",
        payload={"ok": False, "code": "scope_denied", "required_scope": required},
    )


def _approval_payload(row: dict[str, Any], *, code: str = "approval_required") -> dict[str, Any]:
    return {
        "ok": False,
        "code": code,
        "approval_required": code == "approval_required",
        "approval_id": row.get("id", ""),
        "risk": row.get("risk", ""),
        "required_scope": row.get("required_scope", ""),
        "summary": row.get("summary", ""),
        "status": row.get("status", "pending"),
        "expires_at": row.get("expires_at", ""),
        "message": (
            "该操作尚未执行。请在 OpenNexus 设置 → WorkBuddy / MCP 接入中批准，"
            "然后使用完全相同的参数并附带 approval_id 再次调用。"
        ),
    }


def _approval_hook(context: ToolHookContext) -> ToolDecision | None:
    if not context.policy.approval_required:
        return None
    user_id = int(context.identity["user_id"])
    token_id = int(context.identity.get("id") or 0)
    digest = arguments_hash(context.arguments)
    if context.approval_id:
        consumed = db.consume_mcp_tool_approval(
            user_id, token_id, context.approval_id, context.tool_name, digest,
        )
        if consumed:
            return None
        existing = db.get_mcp_tool_approval(user_id, context.approval_id)
        if existing and existing.get("status") == "pending":
            return ToolDecision(action="ask", message="操作仍在等待批准", payload=_approval_payload(existing))
        message = "审批单无效、已拒绝、已过期或已使用；该操作未执行"
        return ToolDecision(
            action="ask", message=message,
            payload={
                "ok": False, "code": "approval_invalid", "approval_required": False,
                "approval_id": context.approval_id, "message": message,
            },
        )
    row = db.create_mcp_tool_approval(
        user_id=user_id,
        token_id=token_id,
        tool_name=context.tool_name,
        arguments_hash=digest,
        arguments=redacted_arguments(context.arguments),
        risk=context.policy.risk,
        required_scope=context.policy.required_scope,
        summary=approval_summary(context.tool_name, context.arguments),
    )
    context.approval_id = str(row["id"])
    return ToolDecision(action="ask", message="操作需要批准", payload=_approval_payload(row))


def _audit_hook(context: ToolHookContext, result: dict[str, Any] | None, error: str) -> None:
    duration_ms = round((time.perf_counter() - context.started_at) * 1000)
    success = not error and context.decision.action == "allow"
    if isinstance(result, dict) and result.get("ok") is False:
        success = False
    decision = "executed" if success else (
        context.decision.action if context.decision.action != "allow" else "failed"
    )
    audit_error = error
    if not audit_error and isinstance(result, dict) and result.get("ok") is False:
        audit_error = str(result.get("message") or result.get("code") or "工具未执行")
    db.add_mcp_audit_log(
        int(context.identity["user_id"]), context.identity.get("id"), context.tool_name,
        redacted_arguments(context.arguments), success, audit_error, duration_ms,
        risk=context.policy.risk, decision=decision,
        approval_id=context.approval_id, required_scope=context.policy.required_scope,
    )


register_pre_tool_hook(_scope_hook)
register_pre_tool_hook(_approval_hook)
register_post_tool_hook(_audit_hook)
