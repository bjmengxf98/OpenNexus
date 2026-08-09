"""兼容旧会话接口的 HTML 渲染函数，不依赖任何界面框架。"""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

import markdown


_md = markdown.Markdown(extensions=["tables", "fenced_code", "nl2br"])


def user_bubble(text: str) -> str:
    parts = re.split(r"\n*【文件：([^】]+)】\n", text or "")
    if len(parts) == 1:
        escaped = html.escape(text or "")
    else:
        display = [html.escape(parts[0].strip())] if parts[0].strip() else []
        display.extend(f"📎 {html.escape(parts[i])}" for i in range(1, len(parts), 2))
        escaped = "\n".join(display)
    return f'<div class="row-user"><div class="bubble-user">{escaped}</div></div>'


def ai_bubble(text: str) -> str:
    _md.reset()
    rendered = _md.convert(text or "")
    return f'<div class="row-ai"><div class="ai-icon">✦</div><div class="bubble-ai">{rendered}</div></div>'


def format_timestamp(ts: str, prev_ts: str | None = None) -> str:
    if not ts:
        return ""
    try:
        current = datetime.fromisoformat(ts)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        cst = timezone(timedelta(hours=8))
        current = current.astimezone(cst)
        if prev_ts:
            previous = datetime.fromisoformat(prev_ts)
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
            if (current - previous.astimezone(cst)).total_seconds() < 300:
                return ""
        now = datetime.now(cst)
        label = current.strftime("%H:%M") if current.date() == now.date() else current.strftime("%m月%d日 %H:%M" if current.year == now.year else "%Y年%m月%d日 %H:%M")
        return f'<div class="ts-mark">{label}</div>'
    except Exception:
        return ""
