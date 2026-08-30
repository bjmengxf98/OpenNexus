"""任务规划、按需工具发现和保守上下文预算。

本模块只决定“哪些工具定义呈现给模型”，不改变任何业务工具的执行函数。
智能路由关闭时，调用方可以继续把完整工具表直接交给模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Iterable


DISCOVER_TOOL_NAME = "discover_tools"
UPDATE_PLAN_TOOL_NAME = "update_task_plan"


@dataclass(frozen=True)
class ToolGroup:
    label: str
    description: str
    keywords: tuple[str, ...]
    tools: tuple[str, ...]


TOOL_GROUPS: dict[str, ToolGroup] = {
    "dbsheet_read": ToolGroup(
        "多维表格查询",
        "读取表结构、记录、进展、统计和变更日志",
        (
            "多维表格", "表格", "记录", "数据", "查询", "查看", "查一下",
            "统计", "汇总", "总结", "进展", "任务", "项目", "负责人",
            "完成情况", "今年", "全年", "本月", "本周", "所有", "全部",
            "变化", "变更", "更新了什么", "操作记录",
        ),
        ("get_schema", "list_records", "analyze_records", "get_change_log"),
    ),
    "dbsheet_records_write": ToolGroup(
        "多维表格记录操作",
        "新增、修改、删除记录以及上传附件",
        (
            "建立任务", "新建任务", "创建任务", "添加任务", "建立项目",
            "新建项目", "创建项目", "新增记录", "添加记录", "录入", "填报",
            "修改记录", "更新记录", "完成任务", "删除记录", "上传附件",
            "添加附件", "写入表格",
        ),
        ("create_records", "update_records", "delete_records", "upload_and_attach"),
    ),
    "dbsheet_design": ToolGroup(
        "多维表格结构",
        "创建或删除工作表、字段和视图",
        (
            "工作表", "字段", "列", "视图", "表结构", "新增字段", "创建字段",
            "修改字段", "删除字段", "创建视图", "删除视图", "看板视图",
            "日历视图",
        ),
        (
            "get_schema", "create_sheet", "delete_sheet", "create_fields",
            "update_fields", "delete_fields", "create_view", "delete_view",
        ),
    ),
    "dashboards_forms": ToolGroup(
        "仪表盘与表单",
        "查询、复制仪表盘，以及读取或修改表单",
        ("仪表盘", "看板", "表单", "问卷", "必填项", "表单字段"),
        (
            "list_dashboards", "copy_dashboard", "get_form_meta",
            "update_form_meta", "list_form_fields", "update_form_field",
        ),
    ),
    "relationships": ToolGroup(
        "父子记录",
        "管理多维表格记录的父子关系",
        ("父子", "子任务", "父任务", "下级任务", "绑定子记录", "解绑子记录"),
        (
            "get_parent_status", "enable_parent", "disable_parent",
            "list_children", "bind_children", "unbind_children",
        ),
    ),
    "hooks": ToolGroup(
        "Webhook",
        "查询、创建和删除多维表格 Webhook",
        ("webhook", "回调", "钩子", "订阅变更", "事件通知"),
        ("list_hooks", "create_hook", "delete_hook"),
    ),
    "knowledge_memory": ToolGroup(
        "知识与记忆",
        "查询单位知识库或保存用户明确要求记住的信息",
        (
            "制度", "规定", "规范", "流程", "办法", "知识库", "单位要求",
            "记住", "记下来", "保存记忆", "以后记得",
        ),
        ("search_knowledge", "save_memory"),
    ),
    "people_messaging": ToolGroup(
        "人员与消息",
        "查询联系人或系统用户，并通过邮件、WPS、企业微信、个人微信发送消息",
        (
            "联系人", "系统用户", "人员", "发消息", "发送消息", "通知",
            "发邮件", "邮件通知", "WPS私信", "WPS消息", "企业微信",
            "发微信", "微信通知", "微信告诉", "微信发",
        ),
        (
            "list_system_users", "list_wps_contacts", "send_notification",
            "send_wps_bot_message", "send_wecom_message", "send_weixin_message",
        ),
    ),
    "spreadsheets": ToolGroup(
        "传统电子表格",
        "创建或管理工作簿、工作表和单元格区域",
        (
            "excel", "xlsx", "电子表格", "工作簿", "单元格", "区域数据",
            "复制工作表", "读取区域", "写入区域", "查找区域",
        ),
        (
            "sheets_create_file", "sheets_list_worksheets",
            "sheets_create_worksheet", "sheets_update_worksheet",
            "sheets_delete_worksheets", "sheets_copy_worksheet",
            "sheets_get_range", "sheets_update_range", "sheets_delete_range",
            "sheets_find_range",
        ),
    ),
    "documents": ToolGroup(
        "文档生成",
        "生成报告、总结、纪要、通知或 Word 文档",
        (
            "生成报告", "写报告", "生成纪要", "写纪要", "生成通知", "写通知",
            "生成文档", "写文档", "生成word", "写word", "导出word", "写总结",
        ),
        ("generate_document",),
    ),
    "reminders": ToolGroup(
        "定时提醒",
        "建立、查询和取消个人提醒",
        (
            "提醒我", "到时提醒", "到时候提醒", "到点提醒", "设置提醒",
            "建立提醒", "我的提醒", "查看提醒", "取消提醒", "有哪些提醒",
            "提醒列表", "查提醒", "待触发提醒", "已设置的提醒",
        ),
        ("add_reminder", "list_reminders", "cancel_reminder"),
    ),
}


TOOL_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "list_records": ("get_schema",),
    "analyze_records": ("get_schema",),
    "create_records": ("get_schema", "list_records"),
    "update_records": ("get_schema", "list_records"),
    "delete_records": ("get_schema", "list_records"),
    "upload_and_attach": ("get_schema", "list_records"),
    "send_notification": ("list_system_users",),
    "send_wps_bot_message": ("list_wps_contacts",),
    "get_change_log": ("get_schema",),
}


_COMPLEX_TASK_KEYWORDS = (
    "总结", "汇总", "分析", "全年", "今年", "所有", "全部", "完整",
    "逐项", "批量", "跨表", "梳理", "制定", "规划", "对比", "综合",
)


def _capability_catalog_text() -> str:
    return "\n".join(
        f"- {key}（{group.label}）：{group.description}"
        for key, group in TOOL_GROUPS.items()
    )


DISCOVER_TOOL = {
    "type": "function",
    "function": {
        "name": DISCOVER_TOOL_NAME,
        "description": (
            "按当前任务搜索并加载尚未展示的业务工具。需要操作系统、查询业务数据，"
            "但当前工具不够时必须调用；可重复搜索不同能力，搜索结果会从下一轮开始可用。"
            "不要因为当前没看到某个工具就声称系统没有此能力。\n\n可搜索的能力组：\n"
            + _capability_catalog_text()
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "用自然语言描述需要的能力、数据或操作",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多加载多少个匹配工具，默认12，最大30",
                },
            },
            "required": ["query"],
        },
    },
}


UPDATE_PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": UPDATE_PLAN_TOOL_NAME,
        "description": (
            "为复杂任务建立并更新可验证的执行计划。任务有多个步骤、需要读取全部数据、"
            "跨表查询或生成综合结果时使用。每次发送完整计划；任务未完成时继续执行，"
            "不能因为工具调用次数多而停止。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "goal": {"type": "string", "description": "用户要求达成的最终目标"},
                "status": {
                    "type": "string",
                    "enum": ["in_progress", "completed", "blocked"],
                    "description": "整个任务的当前状态",
                },
                "steps": {
                    "type": "array",
                    "description": "完整步骤列表",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "blocked"],
                            },
                        },
                        "required": ["content", "status"],
                    },
                },
                "completion_criteria": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "判断任务真正完成所必须满足的条件",
                },
                "findings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "已经从工具结果验证出的阶段性事实。读取大表批次后先写入这里，"
                        "系统才可安全压缩旧的原始工具结果。"
                    ),
                },
                "data_scope": {
                    "type": "object",
                    "description": (
                        "数据范围说明，例如时间范围、表格、字段、是否要求全部记录和全部分页"
                    ),
                    "additionalProperties": True,
                },
                "blocked_reason": {
                    "type": "string",
                    "description": "仅当确实无法继续时说明阻碍原因",
                },
            },
            "required": ["goal", "status", "steps", "completion_criteria"],
        },
    },
}


def tool_name(tool: dict[str, Any]) -> str:
    return str(tool.get("function", {}).get("name", ""))


def tool_names(tools: Iterable[dict[str, Any]]) -> set[str]:
    return {name for tool in tools if (name := tool_name(tool))}


def is_complex_task(user_text: str) -> bool:
    text = (user_text or "").strip().lower()
    return len(text) >= 80 or any(keyword in text for keyword in _COMPLEX_TASK_KEYWORDS)


def _expand_dependencies(names: Iterable[str]) -> set[str]:
    expanded = {name for name in names if name}
    pending = list(expanded)
    while pending:
        name = pending.pop()
        for dependency in TOOL_DEPENDENCIES.get(name, ()):
            if dependency not in expanded:
                expanded.add(dependency)
                pending.append(dependency)
    return expanded


def initial_tool_names(user_text: str, forced_names: Iterable[str] = ()) -> set[str]:
    """按明确意图预加载常用工具；遗漏时由 discover_tools 继续补充。"""
    text = (user_text or "").strip().lower()
    selected = set(forced_names)
    for group in TOOL_GROUPS.values():
        if any(keyword.lower() in text for keyword in group.keywords):
            selected.update(group.tools)
    return _expand_dependencies(selected)


def _search_tokens(text: str) -> set[str]:
    normalized = (text or "").lower().replace("_", " ")
    tokens = set(re.findall(r"[a-z0-9]+", normalized))
    chinese_runs = re.findall(r"[\u3400-\u9fff]+", normalized)
    for run in chinese_runs:
        tokens.update(run)
        tokens.update(run[index:index + 2] for index in range(max(0, len(run) - 1)))
    return {token for token in tokens if token}


def discover_tool_names(
    query: str,
    all_tools: list[dict[str, Any]],
    limit: int = 12,
) -> tuple[list[str], bool]:
    """从能力组和工具元数据中搜索；无匹配时退回全部工具，保证能力不丢失。"""
    query = (query or "").strip()
    limit = max(1, min(int(limit or 12), 30))
    available = tool_names(all_tools)
    selected: list[str] = []

    def add(name: str) -> None:
        if name in available and name not in selected:
            selected.append(name)

    lowered = query.lower()
    for key, group in TOOL_GROUPS.items():
        if (
            key.lower() in lowered
            or group.label.lower() in lowered
            or any(keyword.lower() in lowered for keyword in group.keywords)
        ):
            for name in group.tools:
                add(name)

    query_tokens = _search_tokens(query)
    scored: list[tuple[int, str]] = []
    for tool in all_tools:
        name = tool_name(tool)
        function = tool.get("function", {})
        description = str(function.get("description", ""))
        haystack = f"{name} {description}".lower()
        score = 0
        if name.lower() in lowered or lowered in name.lower():
            score += 100
        score += 4 * len(query_tokens & _search_tokens(haystack))
        for token in query_tokens:
            if len(token) >= 2 and token in haystack:
                score += 2
        if score:
            scored.append((score, name))
    for _score, name in sorted(scored, key=lambda item: (-item[0], item[1])):
        add(name)

    if not selected:
        return [tool_name(tool) for tool in all_tools], True

    selected = selected[:limit]
    expanded = _expand_dependencies(selected)
    ordered = [tool_name(tool) for tool in all_tools if tool_name(tool) in expanded]
    return ordered, False


def visible_tools(
    all_tools: list[dict[str, Any]],
    selected_names: Iterable[str],
    *,
    include_discovery: bool,
    include_plan: bool,
) -> list[dict[str, Any]]:
    selected = set(selected_names)
    result: list[dict[str, Any]] = []
    if include_discovery:
        result.append(DISCOVER_TOOL)
    if include_plan:
        result.append(UPDATE_PLAN_TOOL)
    result.extend(tool for tool in all_tools if tool_name(tool) in selected)
    return result


def discovery_result(
    names: list[str],
    all_tools: list[dict[str, Any]],
    fallback_all: bool,
) -> dict[str, Any]:
    descriptions = {
        tool_name(tool): str(tool.get("function", {}).get("description", ""))[:120]
        for tool in all_tools
        if tool_name(tool) in names
    }
    return {
        "ok": True,
        "loaded_tools": names,
        "count": len(names),
        "fallback_all": fallback_all,
        "capabilities": descriptions,
        "message": (
            "没有找到可靠匹配，已加载全部业务工具以保证功能完整。"
            if fallback_all
            else "匹配工具已加载，可从下一轮开始调用；如仍不足可再次搜索。"
        ),
    }


@dataclass
class TaskPlanState:
    goal: str = ""
    status: str = ""
    steps: list[dict[str, str]] = field(default_factory=list)
    completion_criteria: list[str] = field(default_factory=list)
    data_scope: dict[str, Any] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    blocked_reason: str = ""

    def update(self, arguments: dict[str, Any]) -> dict[str, Any]:
        goal = str(arguments.get("goal") or "").strip()
        status = str(arguments.get("status") or "").strip()
        raw_steps = arguments.get("steps")
        raw_criteria = arguments.get("completion_criteria")
        if not goal:
            raise ValueError("goal 不能为空")
        if status not in {"in_progress", "completed", "blocked"}:
            raise ValueError("status 必须是 in_progress、completed 或 blocked")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("steps 必须是非空列表")
        steps: list[dict[str, str]] = []
        for item in raw_steps:
            if not isinstance(item, dict):
                raise ValueError("每个步骤必须是对象")
            content = str(item.get("content") or "").strip()
            step_status = str(item.get("status") or "").strip()
            if not content or step_status not in {
                "pending", "in_progress", "completed", "blocked",
            }:
                raise ValueError("步骤必须包含 content 和有效 status")
            steps.append({"content": content, "status": step_status})
        if (
            not isinstance(raw_criteria, list) or not raw_criteria or not all(
                isinstance(item, str) and item.strip() for item in raw_criteria
            )
        ):
            raise ValueError("completion_criteria 必须是非空字符串列表")
        if status == "completed" and any(
            item["status"] != "completed" for item in steps
        ):
            raise ValueError("任务标记 completed 前，所有步骤必须 completed")
        blocked_reason = str(arguments.get("blocked_reason") or "").strip()
        raw_findings = arguments.get("findings") if "findings" in arguments else None
        if raw_findings is not None and (not isinstance(raw_findings, list) or not all(
            isinstance(item, str) for item in raw_findings
        )):
            raise ValueError("findings 必须是字符串列表")
        if status == "blocked" and not blocked_reason:
            raise ValueError("任务 blocked 时必须提供 blocked_reason")

        self.goal = goal
        self.status = status
        self.steps = steps
        self.completion_criteria = [str(item).strip() for item in raw_criteria]
        data_scope = arguments.get("data_scope")
        self.data_scope = dict(data_scope) if isinstance(data_scope, dict) else {}
        if raw_findings is not None:
            self.findings = [
                str(item).strip() for item in raw_findings if str(item).strip()
            ][-80:]
        self.blocked_reason = blocked_reason
        return self.as_result()

    @property
    def terminal(self) -> bool:
        return self.status in {"completed", "blocked"}

    @property
    def unfinished(self) -> list[str]:
        return [
            item["content"] for item in self.steps
            if item["status"] not in {"completed", "blocked"}
        ]

    def as_result(self) -> dict[str, Any]:
        return {
            "ok": True,
            "goal": self.goal,
            "status": self.status,
            "steps": self.steps,
            "completion_criteria": self.completion_criteria,
            "data_scope": self.data_scope,
            "blocked_reason": self.blocked_reason,
            "findings": self.findings,
            "unfinished": self.unfinished,
        }


def estimate_text_tokens(value: Any) -> int:
    """对中英文混合内容进行保守估算，避免沿用“中文2字符/token”的低估。"""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    ascii_chars = sum(ord(char) < 128 for char in value)
    non_ascii_chars = len(value) - ascii_chars
    return max(1, (ascii_chars + 3) // 4 + non_ascii_chars)


def estimate_tools_tokens(tools: list[dict[str, Any]]) -> int:
    return estimate_text_tokens(tools) + 8 * len(tools)


def trim_history_to_budget(
    history: list[dict[str, Any]],
    *,
    system_prompt: str,
    tools: list[dict[str, Any]],
    context_window: int,
    max_output_tokens: int,
) -> list[dict[str, Any]]:
    """只裁剪旧的普通对话；始终保留当前用户消息和系统提示。"""
    if not context_window or len(history) <= 1:
        return list(history)
    safe_input_limit = max(2048, int(context_window * 0.80) - max_output_tokens)
    fixed = estimate_text_tokens(system_prompt) + estimate_tools_tokens(tools)
    history_budget = max(1024, safe_input_limit - fixed)
    trimmed = list(history)

    def history_tokens() -> int:
        return sum(
            estimate_text_tokens(message.get("content", "")) + 6
            for message in trimmed
        )

    while len(trimmed) > 1 and history_tokens() > history_budget:
        trimmed.pop(0)
        # 不让裁剪后的历史从孤立的 assistant 消息开始。
        if len(trimmed) > 1 and trimmed[0].get("role") == "assistant":
            trimmed.pop(0)
    return trimmed


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    return estimate_text_tokens(messages) + 6 * len(messages)


def _compact_tool_receipt(content: Any) -> str:
    try:
        data = json.loads(content) if isinstance(content, str) else content
    except (TypeError, json.JSONDecodeError):
        data = {"preview": str(content)[:500]}
    if not isinstance(data, dict):
        data = {"preview": str(data)[:500]}
    keep_keys = (
        "ok", "error", "message", "total", "fetched", "has_more",
        "next_page_token", "is_complete", "source_records", "matched_records",
        "pages_fetched", "source_is_complete", "returned_rows",
        "next_row_offset", "continuation_error", "_note", "_completeness",
    )
    receipt = {
        key: data[key] for key in keep_keys
        if key in data
    }
    receipt["_compacted"] = True
    receipt["_instruction"] = (
        "原始明细已在阶段性 findings 保存后压缩；如仍需明细，请按原范围重新查询。"
    )
    return json.dumps(receipt, ensure_ascii=False, separators=(",", ":"), default=str)


def compact_tool_messages(
    messages: list[dict[str, Any]],
    *,
    preserve_recent_tool_messages: int = 2,
    min_chars: int = 2500,
) -> tuple[list[dict[str, Any]], int]:
    """压缩较旧的大型工具结果，同时保留 assistant/tool 配对结构。"""
    result = [dict(message) for message in messages]
    tool_indexes = [
        index for index, message in enumerate(result)
        if message.get("role") == "tool"
    ]
    preserve = max(0, preserve_recent_tool_messages)
    protected = set(tool_indexes[-preserve:]) if preserve else set()
    compacted = 0
    for index in tool_indexes:
        if index in protected:
            continue
        content = str(result[index].get("content") or "")
        if len(content) < max(200, min_chars):
            continue
        result[index]["content"] = _compact_tool_receipt(content)
        compacted += 1
    return result, compacted


def compact_messages_for_retry(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """上下文超限后的保守降载：丢弃旧对话，保留本轮工具链并压缩大型结果。"""
    if not messages:
        return [], 0
    first_tool_call = next(
        (
            index for index, message in enumerate(messages)
            if message.get("role") == "assistant" and message.get("tool_calls")
        ),
        None,
    )
    if first_tool_call is None:
        user_indexes = [
            index for index, message in enumerate(messages)
            if message.get("role") == "user"
        ]
        run_start = user_indexes[-1] if user_indexes else 1
    else:
        user_indexes = [
            index for index in range(first_tool_call)
            if messages[index].get("role") == "user"
        ]
        run_start = user_indexes[-1] if user_indexes else 1
    reduced = [dict(messages[0])] + [
        dict(message) for message in messages[max(1, run_start):]
    ]
    return compact_tool_messages(
        reduced,
        preserve_recent_tool_messages=0,
        min_chars=1200,
    )


def is_context_limit_error(error: Exception) -> bool:
    text = str(error or "").lower()
    markers = (
        "context_length_exceeded",
        "maximum context length",
        "max context length",
        "input tokens exceed",
        "tokens exceed the configured limit",
        "prompt is too long",
        "request too large",
        "too many tokens",
        "上下文长度",
        "输入 token",
        "输入token",
    )
    return any(marker in text for marker in markers)


@dataclass
class RunMetrics:
    full_tool_count: int = 0
    full_tool_tokens: int = 0
    model_requests: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    context_compactions: int = 0
    context_recoveries: int = 0
    last_visible_tool_count: int = 0
    last_visible_tool_tokens: int = 0
    estimated: bool = False

    def record_response(
        self,
        response: Any,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> None:
        self.model_requests += 1
        self.last_visible_tool_count = len(tools)
        self.last_visible_tool_tokens = estimate_tools_tokens(tools)
        usage = getattr(response, "usage", None)
        prompt = getattr(usage, "prompt_tokens", None) if usage is not None else None
        completion = (
            getattr(usage, "completion_tokens", None) if usage is not None else None
        )
        if prompt is None and isinstance(usage, dict):
            prompt = usage.get("prompt_tokens") or usage.get("input_tokens")
        if completion is None and isinstance(usage, dict):
            completion = usage.get("completion_tokens") or usage.get("output_tokens")
        if prompt is None:
            prompt = estimate_messages_tokens(messages) + estimate_tools_tokens(tools)
            self.estimated = True
        if completion is None:
            choices = getattr(response, "choices", None) or []
            message = getattr(choices[0], "message", None) if choices else None
            payload = {
                "content": getattr(message, "content", "") if message else "",
                "tool_calls": [
                    {
                        "name": getattr(getattr(call, "function", None), "name", ""),
                        "arguments": getattr(
                            getattr(call, "function", None), "arguments", ""
                        ),
                    }
                    for call in (getattr(message, "tool_calls", None) or [])
                ],
            }
            completion = estimate_text_tokens(payload)
            self.estimated = True
        self.prompt_tokens += max(0, int(prompt or 0))
        self.completion_tokens += max(0, int(completion or 0))

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def snapshot(self) -> dict[str, Any]:
        saved = max(0, self.full_tool_tokens - self.last_visible_tool_tokens)
        saving_rate = (
            round(saved / self.full_tool_tokens * 100, 1)
            if self.full_tool_tokens else 0.0
        )
        return {
            "model_requests": self.model_requests,
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated": self.estimated,
            "visible_tools": self.last_visible_tool_count,
            "full_tools": self.full_tool_count,
            "tool_definition_tokens_saved": saved,
            "tool_definition_saving_rate": saving_rate,
            "context_compactions": self.context_compactions,
            "context_recoveries": self.context_recoveries,
        }
