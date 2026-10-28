"""Shared owner-scoped memory management for chat and MCP; no WPS calls."""
from auth import db
from core.context_memory import sanitize_memory_content


MEMORY_TOOL_NAMES = {"list_memories", "delete_memory", "replace_memory"}
MEMORY_TOOLS = [
    {"type": "function", "function": {
        "name": "list_memories",
        "description": "查询当前用户已保存的记忆，包括旧版兼容记忆。修改或删除前先查询，使用返回的 memory_id，不猜编号。支持关键词和分页。",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {
            "query": {"type": "string", "description": "要查找的记忆关键词；省略则列出本人记忆"},
            "offset": {"type": "integer", "minimum": 0, "description": "续读使用上次返回的 next_offset"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        }},
    }},
    {"type": "function", "function": {
        "name": "delete_memory",
        "description": "仅在用户明确要求删除、忘记或作废旧记忆时使用。先 list_memories 定位，再传准确 memory_id；移除该内容的本人重复副本及旧版兼容副本，保留删除标记防止自动学回。不删除历史聊天或 WPS 记录。对象有歧义时先询问。",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {
            "memory_id": {"type": "string", "description": "list_memories 返回的完整 memory_id，包含内容版本"},
        }, "required": ["memory_id"]},
    }},
    {"type": "function", "function": {
        "name": "replace_memory",
        "description": "用户明确要求用新内容替换旧记忆时，先查询并定位旧记忆，再原子替换，保留原范围。新内容校验失败会保留旧记忆。必须以 verified=true 回执确认完成，不要先删除再保存。",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {
            "memory_id": {"type": "string", "description": "list_memories 返回的旧记忆完整 memory_id"},
            "content": {"type": "string", "description": "用户明确认可的新记忆内容"},
        }, "required": ["memory_id", "content"]},
    }},
]


def execute_memory_tool(name: str, args: dict, user_id: int) -> dict:
    if not user_id or int(user_id) <= 0:
        raise ValueError("无法获取当前用户，不能操作记忆")
    allowed = {"query", "offset", "limit"} if name == "list_memories" else {"memory_id"}
    if name == "replace_memory":
        allowed.add("content")
    if name not in MEMORY_TOOL_NAMES or set(args) - allowed:
        raise ValueError("记忆工具或参数无效")
    if name == "list_memories":
        query = args.get("query") or ""
        if not isinstance(query, str):
            raise ValueError("记忆查询关键词必须是文本")
        return db.list_manageable_memories(user_id, query=query.strip(),
                                           offset=args.get("offset", 0), limit=args.get("limit", 30))
    memory_id = args.get("memory_id")
    if not isinstance(memory_id, str) or not memory_id:
        raise ValueError("请先查询并提供准确的 memory_id")
    replacement = None
    if name == "replace_memory":
        if not isinstance(args.get("content"), str):
            raise ValueError("新记忆内容必须是文本")
        replacement, rejected = sanitize_memory_content(args["content"])
        if not replacement or rejected:
            raise ValueError("新记忆为空或包含不能保存的实时配置，旧记忆保持不变")
    result = db.change_user_memory(user_id, memory_id, replacement=replacement)
    result["message"] = ("记忆已替换并核验" if name == "replace_memory" else
                         "该记忆已移除" if result.get("already_deleted") else "记忆已删除并核验")
    return result
