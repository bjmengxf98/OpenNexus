"""多用户、多话题、多数据源的轻量上下文组装。

WPS 仍是业务事实源；本模块只保存用户确认的长期信息、话题摘要和历史消息索引，
不会复制任务/项目等易变业务数据。任何召回失败都返回旧记忆，保证聊天主链路可降级。
"""
from __future__ import annotations

import re
from typing import Iterable

from auth import db


_CONFIG_PATTERNS = (
    re.compile(r"\b(?:file_?id|sheet_?id|record_?id)\b", re.I),
    re.compile(r"(?:默认|当前).{0,5}(?:表格|多维表|数据源)"),
    re.compile(r"(?:把|将).{0,20}(?:设为|设置为).{0,4}默认"),
)
_STOP_TOKENS = {
    "今天", "明天", "现在", "这个", "那个", "一下", "什么", "怎么", "是否",
    "可以", "需要", "帮我", "我们", "你们", "他们", "进行", "已经", "还有",
}


def sanitize_memory_content(content: str) -> tuple[str, list[str]]:
    """过滤不应持久化的实时配置，返回（安全内容，被过滤行）。"""
    kept: list[str] = []
    rejected: list[str] = []
    for raw_line in str(content or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(pattern.search(line) for pattern in _CONFIG_PATTERNS):
            rejected.append(line)
        else:
            kept.append(line)
    return "\n".join(kept).strip(), rejected


def _tokens(text: str) -> set[str]:
    text = str(text or "").lower()
    result = set(re.findall(r"[a-z0-9_\-]{2,}", text))
    for segment in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        if segment not in _STOP_TOKENS and len(segment) <= 10:
            result.add(segment)
        # 中文没有空格，使用 2～4 字片段支持近似业务语义召回。
        for size in (2, 3, 4):
            if len(segment) < size:
                continue
            for index in range(len(segment) - size + 1):
                token = segment[index:index + size]
                if token not in _STOP_TOKENS:
                    result.add(token)
    return result


def _safe_recall_content(content: str) -> str:
    """历史召回不暴露附件解析正文、临时路径或内部系统提示。"""
    raw = str(content or "")
    visible = re.split(
        r"\n*\u3010(?:\u6587\u4ef6|\u56fe\u7247)\uff1a",
        raw,
        maxsplit=1,
    )[0].strip()
    return re.sub(
        r"\[\u7cfb\u7edf\u63d0\u793a\uff1a.*",
        "",
        visible,
        flags=re.S,
    ).strip()


def recall_chat_history(user_id: int, query: str, *, conversation_id: int = 0,
                        recent_contents: Iterable[str] = (), limit: int = 4,
                        max_chars: int = 1600) -> list[dict]:
    """从该用户自己的历史原话中召回相关片段，不读取其他用户或 AI 回复。"""
    query = str(query or "").strip()
    query_tokens = _tokens(query)
    if not query_tokens:
        return []
    recent_normalized = {str(value or "").strip() for value in recent_contents}
    scored: list[tuple[float, int, dict]] = []
    candidates = db.get_user_chat_candidates(user_id, limit=600)
    total = max(len(candidates), 1)
    for position, row in enumerate(candidates):
        raw_content = str(row.get("content") or "").strip()
        content = _safe_recall_content(raw_content)
        if not content or raw_content == query or raw_content in recent_normalized or content == query:
            continue
        candidate_tokens = _tokens(content)
        overlap = query_tokens & candidate_tokens
        if not overlap:
            continue
        score = sum(min(len(token), 4) for token in overlap)
        if query in content or content in query:
            score += 8
        title = str(row.get("conversation_title") or "")
        score += 0.75 * len(query_tokens & _tokens(title))
        # 相同话题可召回更早内容，但不给它压倒关键词相关度的权重。
        if conversation_id and int(row.get("conversation_id") or 0) == int(conversation_id):
            score += 1.5
        score += max(0.0, (total - position) / total) * 0.5
        if score >= 3:
            scored.append((score, int(row.get("id") or 0), row))

    selected: list[dict] = []
    used_chars = 0
    for _score, _row_id, row in sorted(scored, key=lambda item: (item[0], item[1]), reverse=True):
        content = _safe_recall_content(str(row.get("content") or "").strip())
        snippet = content[:500]
        if used_chars + len(snippet) > max_chars and selected:
            continue
        selected.append({**row, "content": snippet, "score": round(_score, 2)})
        used_chars += len(snippet)
        if len(selected) >= limit:
            break
    return selected


def _append_unique(target: list[str], seen: set[str], content: str) -> None:
    normalized = str(content or "").strip().lstrip("- ").strip()
    if not normalized or normalized in seen:
        return
    seen.add(normalized)
    target.append(normalized)


def build_user_context(user_id: int, conversation_id: int, query: str, *,
                       legacy_memory: str = "", default_file: dict | None = None,
                       all_files: list[dict] | None = None,
                       recent_contents: Iterable[str] = ()) -> str:
    """组装模型上下文；异常时仅返回旧记忆，不影响既有功能。"""
    try:
        sections: list[str] = []
        seen: set[str] = set()

        global_lines: list[str] = []
        for item in db.list_memory_items(user_id, scope_type="global", limit=80):
            _append_unique(global_lines, seen, item.get("content", ""))
        for line in str(legacy_memory or "").splitlines():
            _append_unique(global_lines, seen, line)
        if global_lines:
            sections.append("【用户长期记忆】\n" + "\n".join(f"- {line}" for line in global_lines[:80]))

        if conversation_id:
            topic_lines: list[str] = []
            for item in db.list_memory_items(
                user_id, scope_type="conversation", scope_ids=[str(conversation_id)], limit=30,
            ):
                _append_unique(topic_lines, seen, item.get("content", ""))
            if topic_lines:
                sections.append("【当前话题记忆】\n" + "\n".join(f"- {line}" for line in topic_lines))

        # 默认数据源始终生效；用户明确提及其他已连接表格名称时同时加入该表规则。
        relevant_file_ids: list[str] = []
        if default_file and default_file.get("file_id"):
            relevant_file_ids.append(str(default_file["file_id"]))
        for file_info in all_files or []:
            file_id = str(file_info.get("file_id") or "")
            file_name = str(file_info.get("file_name") or "")
            if file_id and file_name and file_name in str(query or "") and file_id not in relevant_file_ids:
                relevant_file_ids.append(file_id)
        if relevant_file_ids:
            file_lines: list[str] = []
            for item in db.list_memory_items(
                user_id, scope_type="file", scope_ids=relevant_file_ids, limit=40,
            ):
                _append_unique(file_lines, seen, item.get("content", ""))
            if file_lines:
                sections.append("【当前数据源业务规则】\n" + "\n".join(f"- {line}" for line in file_lines))

        recalled = recall_chat_history(
            user_id, query, conversation_id=conversation_id,
            recent_contents=recent_contents, limit=4,
        )
        if recalled:
            history_lines = []
            for row in recalled:
                title = str(row.get("conversation_title") or "历史话题")
                date = str(row.get("created_at") or "")[:10]
                history_lines.append(f"- [{date}·{title}] 用户曾说：{row['content']}")
            sections.append(
                "【相关历史原话（仅作为线索，涉及当前业务状态时必须重新查询 WPS）】\n"
                + "\n".join(history_lines)
            )

        result = "\n\n".join(sections).strip()
        return result[:7000]
    except Exception as exc:
        print(f"[CONTEXT] build fallback: {type(exc).__name__}: {exc}")
        return str(legacy_memory or "")
