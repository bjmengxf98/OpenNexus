"""
知识库向量嵌入 API 路由

所有路径前缀 /api/admin/kb/embed…
通过 fastapi_app.include_router(kb_embed_router) 挂载到主应用。

此文件不依赖 app.py 中的任何变量，仅依赖：
  - auth.db（数据库操作）
  - core.knowledge_rag（嵌入工具函数）
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from auth import db
from core.knowledge_rag import embed_knowledge_doc

kb_embed_router = APIRouter()

# 全局嵌入任务状态（模块级，整个进程共享）
_embed_all_state: dict = {
    "running": False,
    "done": 0,
    "total": 0,
    "failed": 0,
    "msg": "",
}


def _is_admin(request: Request) -> bool:
    uid = request.session.get("uid")
    if not uid:
        return False
    user = db.get_user_by_id(uid)
    return bool(user and user.get("is_admin"))


@kb_embed_router.get("/api/admin/kb/embed_config")
async def get_embed_config(request: Request):
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "无权限"})
    cfg = db.get_embed_config() or {}
    return JSONResponse({
        "ok": True,
        "api_key": cfg.get("api_key", ""),
        "base_url": cfg.get("base_url", "https://api.siliconflow.cn/v1"),
        "model": cfg.get("model", "BAAI/bge-m3"),
    })


@kb_embed_router.post("/api/admin/kb/embed_config")
async def save_embed_config(request: Request):
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "无权限"})
    body = await request.json()
    api_key = (body.get("api_key") or "").strip()
    base_url = (body.get("base_url") or "https://api.siliconflow.cn/v1").strip()
    model = (body.get("model") or "BAAI/bge-m3").strip()
    if not api_key:
        return JSONResponse({"ok": False, "error": "api_key 不能为空"})
    db.save_embed_config(api_key, base_url, model)
    return JSONResponse({"ok": True})


@kb_embed_router.get("/api/admin/kb/embed_status")
async def get_embed_status(request: Request):
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "无权限"})
    counts = db.get_chunk_counts()
    return JSONResponse({"ok": True, "counts": counts})


@kb_embed_router.post("/api/admin/kb/embed/{kid}")
async def embed_one_doc(request: Request, kid: int):
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "无权限"})
    items = [i for i in db.list_knowledge(enabled_only=False) if i["id"] == kid]
    if not items:
        return JSONResponse({"ok": False, "error": "文档不存在"})
    doc = items[0]
    n = await embed_knowledge_doc(kid, doc["content"], doc["title"])
    if n < 0:
        return JSONResponse({"ok": False, "error": "未配置嵌入模型或嵌入失败"})
    return JSONResponse({"ok": True, "chunks": n})


@kb_embed_router.post("/api/admin/kb/embed_all")
async def embed_all_docs(request: Request):
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "无权限"})
    if _embed_all_state["running"]:
        return JSONResponse({"ok": False, "error": "已有嵌入任务在运行"})
    if not db.get_embed_config():
        return JSONResponse({"ok": False, "error": "请先配置嵌入模型"})

    async def _run():
        docs = db.list_knowledge(enabled_only=False)
        _embed_all_state.update({
            "running": True, "done": 0, "total": len(docs),
            "failed": 0, "msg": "开始嵌入…",
        })
        for doc in docs:
            _embed_all_state["msg"] = f"正在嵌入《{doc['title']}》…"
            n = await embed_knowledge_doc(doc["id"], doc["content"], doc["title"])
            if n < 0:
                _embed_all_state["failed"] += 1
            _embed_all_state["done"] += 1
        _embed_all_state["running"] = False
        _embed_all_state["msg"] = (
            f"完成：{_embed_all_state['done']} 篇，"
            f"失败 {_embed_all_state['failed']} 篇"
        )

    asyncio.create_task(_run())
    return JSONResponse({"ok": True, "msg": "已启动"})


@kb_embed_router.get("/api/admin/kb/embed_all/status")
async def embed_all_status(request: Request):
    return JSONResponse({**_embed_all_state, "ok": True})
