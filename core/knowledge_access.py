"""Bounded original-text reads and excerpts for the existing knowledge tool."""
from __future__ import annotations

import hashlib
import json

from auth import db

_PAGE_SIZE = 6000
_MAX_PAGE_SIZE = 8000
_EXCERPT_SIZE = 2000


def _window(doc: dict, offset: int, limit: int, scope: str) -> dict:
    text = doc["content"]
    version = hashlib.sha256(json.dumps(
        [doc["id"], doc["title"], doc.get("file_name", ""), text], ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    end = min(len(text), offset + limit)
    return {
        "knowledge_id": doc["id"], "title": doc["title"], "file_name": doc.get("file_name", ""),
        "document_version": version, "total_chars": len(text), "content_scope": scope,
        "content": text[offset:end], "offset": offset, "end_offset": end,
        "is_complete": offset == 0 and end == len(text), "has_more": end < len(text),
        "next_offset": end if end < len(text) else None,
        "read_arguments": {"knowledge_id": doc["id"], "offset": offset,
                           "limit": limit, "version": version},
    }


def knowledge_excerpt(doc: dict, query: str) -> dict:
    """Expose a real matched span, never substitute stale vector chunk text."""
    text = doc["content"]
    chunk = doc.get("chunk_text") or ""
    title_prefix = doc["title"] + "\n"
    if chunk.startswith(title_prefix):
        chunk = chunk[len(title_prefix):]
    hit = text.find(chunk) if chunk else -1
    if hit < 0:
        positions = [text.find(word) for word in query.split()[:5] if word]
        hit = min((pos for pos in positions if pos >= 0), default=0)
    item = _window(doc, max(0, hit - 120), _EXCERPT_SIZE, "search_excerpt")
    # Excerpts can begin in the middle; full reading always starts at the head.
    item["read_arguments"] = {**item["read_arguments"], "offset": 0, "limit": _PAGE_SIZE}
    return item


def knowledge_search_result(rows: list[dict], query: str) -> dict:
    items = [knowledge_excerpt(doc, query) for doc in rows]
    return {"found": bool(items), "count": len(items), "items": items,
            "_knowledge_read": [item["read_arguments"] for item in items],
            "message": "检索返回的是相关原文片段。需完整条文时，用search_knowledge加read_arguments读取原文；不能把片段当全文。"}


def read_knowledge_document(knowledge_id: int, offset: int = 0,
                            limit: int = _PAGE_SIZE, version: str = "") -> dict:
    if (type(knowledge_id) is not int or knowledge_id <= 0
            or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= _MAX_PAGE_SIZE
            or not isinstance(version, str) or (offset > 0 and not version)):
        return {"error": "请提供有效knowledge_id、offset及1至8000的limit；续读必须带回上页version。",
                "code": "invalid_knowledge_read"}
    doc = db.get_enabled_knowledge(knowledge_id)
    if doc is None:
        return {"found": False, "message": "知识条目不存在或未启用，未返回原文。"}
    item = _window(doc, offset, limit, "document_page")
    if version and version != item["document_version"]:
        return {"error": "知识条目已变更，请重新检索或从头读取，不要合并不同版本的片段。",
                "code": "knowledge_version_changed"}
    if offset > item["total_chars"]:
        return {"error": "offset超出当前原文长度，请从头读取。", "code": "invalid_knowledge_read"}
    return {"found": True, "count": 1, "items": [item], "has_more": item["has_more"],
            "next_offset": item["next_offset"], "is_complete": item["is_complete"],
            "_knowledge_read": [item["read_arguments"]],
            "message": "这是已存原文的一页；has_more=true时保留version并以next_offset续读。尾页不等于此前所有页已读。"}
