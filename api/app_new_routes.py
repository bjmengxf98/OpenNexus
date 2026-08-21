"""独立 HTML 主界面及聊天接口。

该模块不依赖任何界面框架。``/`` 是正式入口，``/app-new`` 保留为兼容入口。
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import re
from pathlib import Path

import markdown
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from agent.assistant import Assistant
from auth import db
from auth.wps_oauth import build_auth_url, calc_expires_at, is_token_expired, refresh_access_token
from core import upload_queue
from core.context_memory import build_user_context, sanitize_memory_content
from core.file_parser import parse_file


app_new_router = APIRouter()
_HTML_FILE = Path(__file__).resolve().parent.parent / "static" / "app_new.html"
_HELP_FILE = Path(__file__).resolve().parent.parent / "docs" / "用户帮助.md"
_user_locks: dict[int, asyncio.Lock] = {}
_turn_tasks: dict[str, asyncio.Task] = {}
_turn_subscribers: dict[str, set[asyncio.Queue]] = {}
_TERMINAL_TURN_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
_markdown = markdown.Markdown(extensions=["tables", "fenced_code", "nl2br"])


def _event(kind: str, **payload) -> str:
    data = json.dumps({"type": kind, **payload}, ensure_ascii=False)
    return f"data: {data}\n\n"


def _publish_turn_event(turn_id: str, event: dict):
    """向当前进程内的在线订阅者广播；持久化由调用方先完成。"""
    for queue in tuple(_turn_subscribers.get(turn_id, ())):
        queue.put_nowait(dict(event))


async def _emit_turn_event(turn_id: str, kind: str, **payload) -> dict:
    event_id = db.add_agent_turn_event(turn_id, kind, payload)
    event = {"event_id": event_id, "turn_id": turn_id, "type": kind, **payload}
    _publish_turn_event(turn_id, event)
    return event


async def _stream_turn_events(user_id: int, turn_id: str, after_id: int = 0):
    """先回放数据库事件，再实时跟随；断线后可用事件游标无损续接。"""
    queue: asyncio.Queue[dict] = asyncio.Queue()
    subscribers = _turn_subscribers.setdefault(turn_id, set())
    subscribers.add(queue)
    last_id = max(0, int(after_id))
    try:
        for item in db.list_agent_turn_events(turn_id, user_id, last_id):
            last_id = max(last_id, int(item["event_id"]))
            kind = item.pop("type")
            yield _event(kind, **item)

        while True:
            turn = db.get_agent_turn(turn_id, user_id)
            if not turn:
                return
            # 多进程部署时事件可能由另一进程写入，不能只依赖本地内存队列。
            for item in db.list_agent_turn_events(turn_id, user_id, last_id):
                last_id = max(last_id, int(item["event_id"]))
                kind = item.pop("type")
                yield _event(kind, **item)
            if turn.get("status") in _TERMINAL_TURN_STATUSES:
                return
            try:
                item = await asyncio.wait_for(queue.get(), timeout=15)
            except asyncio.TimeoutError:
                yield _event("ping", turn_id=turn_id, event_id=last_id)
                continue
            if item.get("type") == "close":
                continue
            event_id = int(item.get("event_id") or 0)
            if event_id <= last_id:
                continue
            last_id = event_id
            kind = item.pop("type")
            yield _event(kind, **item)
    finally:
        subscribers.discard(queue)
        if not subscribers:
            _turn_subscribers.pop(turn_id, None)


def _render_markdown(text: str) -> str:
    _markdown.reset()
    return _markdown.convert(text or "")


def _display_user_text(text: str) -> str:
    """历史记录保存的是增强后的全文，展示时隐藏解析内容和服务器路径。"""
    raw = text or ""
    names = re.findall(r"【(?:文件|图片)：([^】]+)】", raw)
    visible = re.split(r"\n*【(?:文件|图片)：", raw, maxsplit=1)[0].strip()
    lines = [visible] if visible else []
    lines.extend(f"📎 {name}" for name in names)
    return "\n".join(lines) or "已上传附件"


def _current_user(request: Request):
    uid = request.session.get("uid")
    if not uid:
        return None, None
    row = db.get_user_by_id(int(uid))
    if not row or not row["is_enabled"]:
        request.session.clear()
        return None, None
    return int(uid), dict(row)


@app_new_router.get("/", response_class=HTMLResponse)
@app_new_router.get("/app-new", response_class=HTMLResponse)
async def app_new_page(request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return RedirectResponse("/login?next=/", status_code=302)
    return HTMLResponse(_HTML_FILE.read_text(encoding="utf-8"))


@app_new_router.get("/api/app-new/bootstrap")
async def app_new_bootstrap(request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)

    conversations = db.list_conversations(uid)
    from core.state import user_current_conv
    current_id = int(user_current_conv.get(uid) or 0)
    if current_id and not db.get_conversation(current_id, uid):
        current_id = 0
    if not current_id and conversations:
        last_active = db.get_last_active_conv_id(uid)
        current_id = int(last_active or conversations[0]["id"])

    token = db.get_wps_token(uid) or {}
    wps_connected = bool(
        token.get("access_token")
        and not is_token_expired(token.get("expires_at", "2000-01-01T00:00:00"))
    )
    files = db.list_wps_files(uid)
    default_file = db.get_default_wps_file(uid)
    active_turn = db.get_active_agent_turn(uid, current_id) if current_id else None
    return {
        "ok": True,
        "user": {
            "id": uid,
            "username": user.get("username", ""),
            "display_name": user.get("display_name") or user.get("username", ""),
            "role": user.get("role", "staff"),
            "is_admin": bool(user.get("is_admin")),
        },
        "wps": {
            "connected": wps_connected,
            "username": token.get("wps_username") or "已连接",
            "connect_url": build_auth_url(uid),
            "files": files,
            "default_file": default_file,
        },
        "conversations": conversations,
        "current_conversation_id": current_id,
        "active_turn": ({
            "id": active_turn["id"],
            "conversation_id": active_turn["conversation_id"],
            "status": active_turn["status"],
        } if active_turn else None),
    }


@app_new_router.get("/api/app-new/help")
async def app_new_help(request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    try:
        source = _HELP_FILE.read_text(encoding="utf-8")
    except OSError:
        return JSONResponse({"ok": False, "error": "帮助文件未找到"}, status_code=404)
    return {"ok": True, "html": _render_markdown(source)}


@app_new_router.get("/api/app-new/conversations/{conv_id}/messages")
async def app_new_messages(conv_id: int, request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    if not db.get_conversation(conv_id, uid):
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)

    from core.state import user_current_conv
    user_current_conv[uid] = conv_id
    rows = db.get_chat_history(uid, conv_id=conv_id, limit=50)
    messages = []
    for row in rows:
        content = row.get("content") or ""
        messages.append({
            "role": row.get("role"),
            "content": _display_user_text(content) if row.get("role") == "user" else content,
            "html": "" if row.get("role") == "user" else _render_markdown(content),
            "created_at": row.get("created_at") or "",
        })
    return {"ok": True, "messages": messages}


@app_new_router.post("/api/app-new/conversations/{conv_id}/clear")
async def app_new_clear_conversation(conv_id: int, request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    if not db.get_conversation(conv_id, uid):
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    db.clear_chat_history(uid, conv_id=conv_id)
    return {"ok": True}


@app_new_router.post("/api/app-new/wps/disconnect")
async def app_new_disconnect_wps(request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    db.save_wps_token(uid, "", "", "2000-01-01T00:00:00", "", "")
    return {"ok": True}


@app_new_router.get("/api/app-new/turns/{turn_id}")
async def app_new_turn_status(turn_id: str, request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    turn = db.get_agent_turn(turn_id, uid)
    if not turn:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    return {
        "ok": True,
        "turn": {
            "id": turn["id"],
            "conversation_id": turn["conversation_id"],
            "status": turn["status"],
            "last_event_id": turn.get("last_event_id", 0),
            "error": turn.get("error", ""),
        },
    }


@app_new_router.get("/api/app-new/turns/{turn_id}/events")
async def app_new_turn_events(turn_id: str, request: Request, after: int = 0):
    uid, user = _current_user(request)
    if not uid or not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    if not db.get_agent_turn(turn_id, uid):
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    header_cursor = request.headers.get("last-event-id", "").strip()
    if header_cursor.isdigit():
        after = max(after, int(header_cursor))
    return StreamingResponse(
        _stream_turn_events(uid, turn_id, after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app_new_router.post("/api/app-new/turns/{turn_id}/cancel")
async def app_new_cancel_turn(turn_id: str, request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    turn = db.get_agent_turn(turn_id, uid)
    if not turn:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    if turn.get("status") in _TERMINAL_TURN_STATUSES:
        return {"ok": True, "turn_id": turn_id, "status": turn["status"]}

    turn = db.request_agent_turn_cancel(turn_id, uid) or turn
    task = _turn_tasks.get(turn_id)
    if task and not task.done():
        task.cancel()
    else:
        await _emit_turn_event(turn_id, "cancelled", message="已停止本次回复")
        turn = db.update_agent_turn_status(turn_id, uid, "cancelled") or turn
        _publish_turn_event(turn_id, {"type": "close"})
    return {"ok": True, "turn_id": turn_id, "status": turn.get("status", "cancel_requested")}


@app_new_router.post("/api/app-new/chat")
async def app_new_chat(request: Request):
    uid, user = _current_user(request)
    if not uid or not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)

    body = await request.json()
    text = str(body.get("text") or "").strip()
    conv_id = int(body.get("conversation_id") or 0)
    as_attachment = bool(body.get("as_attachment"))

    if conv_id and not db.get_conversation(conv_id, uid):
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    if not text and not upload_queue.peek(uid):
        return JSONResponse({"ok": False, "error": "消息不能为空"}, status_code=400)

    if not conv_id:
        title = (text[:15].strip() or "新对话")
        conv_id = db.create_conversation(uid, title)
    else:
        title = (db.get_conversation(conv_id, uid) or {}).get("title") or "新对话"
    from core.state import user_current_conv
    user_current_conv[uid] = conv_id

    lock = _user_locks.setdefault(uid, asyncio.Lock())
    turn = db.create_agent_turn(
        uid, conv_id, {"text": text, "as_attachment": as_attachment},
    )
    turn_id = str(turn["id"])

    async def emit(kind: str, **payload):
        await _emit_turn_event(turn_id, kind, **payload)

    def ensure_not_cancelled():
        current = db.get_agent_turn(turn_id, uid) or {}
        if current.get("cancel_requested"):
            raise asyncio.CancelledError

    async def worker():
        pending_cleanup: list[str] = []
        assistant = None
        try:
            db.update_agent_turn_status(turn_id, uid, "in_progress")
            async with lock:
                ensure_not_cancelled()
                files_to_send = upload_queue.dequeue_all(uid)
                await emit("accepted", conversation_id=conv_id, title=title,
                           files=[f.get("name", "") for f in files_to_send])

                token_row = db.get_wps_token(uid) or {}
                access_token = token_row.get("access_token") or ""
                if access_token and is_token_expired(token_row.get("expires_at", "")):
                    try:
                        refresh_token = token_row.get("refresh_token") or ""
                        if not refresh_token:
                            raise ValueError("缺少 refresh_token")
                        new_token = await refresh_access_token(refresh_token)
                        access_token = new_token["access_token"]
                        db.save_wps_token(
                            uid, access_token, new_token.get("refresh_token", refresh_token),
                            calc_expires_at(new_token["expires_in"]),
                            token_row.get("wps_user_id", ""), token_row.get("wps_username", ""),
                        )
                    except Exception:
                        access_token = ""
                        await emit("notice", message="WPS 授权已过期，请重新连接；普通对话仍可继续。")
                ensure_not_cancelled()

                llm_cfg = db.get_llm_key(uid)
                if not llm_cfg or not llm_cfg.get("api_key"):
                    raise RuntimeError("请先在设置中配置大模型 API Key")

                image_cfg = db.get_image_llm_key(uid) if files_to_send else None
                main_advanced = llm_cfg.get("advanced") or {}
                main_vision_cfg = llm_cfg if main_advanced.get("supports_vision") else None
                image_advanced = (image_cfg or {}).get("advanced") or {}
                fallback_vision_cfg = (
                    image_cfg
                    if image_cfg and image_cfg.get("api_key")
                    and image_advanced.get("supports_vision", True)
                    else None
                )
                vision_cfg = main_vision_cfg or fallback_vision_cfg
                image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
                file_contents: list[str] = []
                for file_info in files_to_send:
                    name = file_info["name"]
                    path = file_info["path"]
                    pending_cleanup.append(path)
                    await emit("file", name=name, status="parsing")
                    if as_attachment:
                        file_contents.append(
                            f'[系统提示：用户上传了文件 {name}，服务器临时路径为 {path}。'
                            f'请直接调用 upload_attachment(file_id=..., file_path="{path}", file_name="{name}") '
                            '将其上传为附件，无需解析内容。]'
                        )
                        continue

                    suffix = Path(name).suffix.lower()
                    if suffix in image_suffixes:
                        use_cfg = vision_cfg
                    elif suffix == ".pdf":
                        use_cfg = vision_cfg
                    else:
                        use_cfg = llm_cfg
                    content = await asyncio.get_running_loop().run_in_executor(
                        None, parse_file, path, name,
                        use_cfg.get("api_key") if use_cfg else None,
                        use_cfg.get("base_url") if use_cfg else None,
                        use_cfg.get("model") if use_cfg else None,
                        (use_cfg.get("advanced") or {}).get("max_output_tokens") if use_cfg else None,
                    )
                    ensure_not_cancelled()
                    if suffix in image_suffixes:
                        file_contents.append(f"【图片：{name}】\n{content}\n")
                    else:
                        file_contents.append(
                            f"【文件：{name}】\n{content}\n"
                            f'[系统提示：此文件的服务器临时路径为 {path}。'
                            f'如需上传到 WPS 附件字段，请调用 upload_attachment('
                            f'file_id=..., file_path="{path}", file_name="{name}")]'
                        )
                    await emit("file", name=name, status="done")

                full_text = text
                if file_contents:
                    full_text = (text + "\n\n" if text else "") + "\n\n".join(file_contents)

                history_rows = db.get_chat_history(uid, conv_id=conv_id, limit=20)
                history = [{"role": row["role"], "content": row["content"]} for row in history_rows]
                history.append({"role": "user", "content": full_text})
                db.add_chat(uid, "user", full_text, conv_id=conv_id)

                all_files = db.list_wps_files(uid)
                default_file = db.get_default_wps_file(uid) or (all_files[0] if all_files else None)
                # 新上下文层只做增量增强：发生任何异常时会自动退回旧 user_memory，
                # 不阻断对话、WPS 工具、提醒或知识库链路。
                memory_context = build_user_context(
                    uid, conv_id, text or full_text,
                    legacy_memory=db.get_user_memory(uid),
                    default_file=default_file, all_files=all_files,
                    recent_contents=[row["content"] for row in history_rows],
                )

                async def on_tool_call(name, args):
                    await emit("tool", name=name)

                assistant = Assistant(
                    api_key=llm_cfg["api_key"],
                    provider=llm_cfg.get("provider", "deepseek"),
                    base_url=llm_cfg.get("base_url"),
                    model=llm_cfg.get("model"),
                    advanced=llm_cfg.get("advanced"),
                )
                reply = await assistant.chat(
                    history, access_token, on_tool_call=on_tool_call,
                    username=user.get("display_name") or user.get("username", "用户"),
                    role=user.get("role", "staff"), default_file=default_file,
                    all_files=all_files, memory=memory_context, uid=uid, conv_id=conv_id,
                )
                ensure_not_cancelled()
                db.add_chat(uid, "assistant", reply, conv_id=conv_id)

                conv = db.get_conversation(conv_id, uid)
                new_title = None
                if conv and conv.get("title") == "新对话" and text:
                    new_title = text[:20].strip()
                    if new_title:
                        db.rename_conversation(conv_id, uid, new_title)

                await emit("done", conversation_id=conv_id, reply=reply,
                           html=_render_markdown(reply), title=new_title or title)
                db.update_agent_turn_status(turn_id, uid, "completed")

                async def update_memory():
                    try:
                        if db.get_chat_count(uid, conv_id=conv_id) % 3 == 0 and assistant:
                            recent_rows = db.get_chat_history(uid, conv_id=conv_id, limit=20)
                            recent = [
                                {"role": r["role"], "content": r["content"]}
                                for r in recent_rows if r["role"] == "user"
                            ]
                            current = db.get_memory_item_by_source(
                                uid, "conversation_summary", str(conv_id),
                            ) or {}
                            memory = await assistant.summarize_memory(
                                recent, current.get("content", ""),
                            )
                            memory, _rejected = sanitize_memory_content(memory)
                            if memory:
                                db.save_memory_item(
                                    uid, memory, scope_type="conversation",
                                    scope_id=str(conv_id), category="summary",
                                    source_type="conversation_summary",
                                    source_id=str(conv_id), confidence=0.8,
                                    replace_source=True,
                                )
                    except Exception:
                        pass
                asyncio.create_task(update_memory())
        except asyncio.CancelledError:
            await emit("cancelled", message="已停止本次回复")
            db.update_agent_turn_status(turn_id, uid, "cancelled")
        except Exception as exc:
            await emit("error", message=f"{type(exc).__name__}: {exc}")
            db.update_agent_turn_status(turn_id, uid, "failed", f"{type(exc).__name__}: {exc}")
        finally:
            for path in pending_cleanup:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            _publish_turn_event(turn_id, {"type": "close"})

    task = asyncio.create_task(worker(), name=f"agent-turn:{turn_id}")
    _turn_tasks[turn_id] = task

    def forget_task(done_task: asyncio.Task):
        _turn_tasks.pop(turn_id, None)
        if done_task.cancelled():
            current = db.get_agent_turn(turn_id, uid) or {}
            if current.get("status") not in _TERMINAL_TURN_STATUSES:
                db.add_agent_turn_event(turn_id, "cancelled", {"message": "已停止本次回复"})
                db.update_agent_turn_status(turn_id, uid, "cancelled")
                _publish_turn_event(turn_id, {"type": "close"})

    task.add_done_callback(forget_task)

    return StreamingResponse(
        _stream_turn_events(uid, turn_id), media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Agent-Turn-ID": turn_id,
        },
    )
