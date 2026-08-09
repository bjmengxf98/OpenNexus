"""提醒推送文案格式化。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta


_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def normalize_reminder_schedule(
    user_text: str,
    remind_at: str,
    event_at: str,
    *,
    now: datetime | None = None,
) -> tuple[str, str]:
    """用用户明确的相对日期校正模型工具参数，并保持原提醒提前量。"""
    current = now or datetime.now()
    text = (user_text or "").strip()
    match = re.search(
        r"(?:^(?:请)?(?:在)?|(?:提醒我|通知我|提醒一下)(?:在)?)"
        r"(?P<relative>今天|今日|明天|明日|后天|大后天)",
        text,
    )
    if not match:
        return remind_at, event_at

    relative = match.group("relative")
    days = {
        "今天": 0, "今日": 0,
        "明天": 1, "明日": 1,
        "后天": 2, "大后天": 3,
    }[relative]
    try:
        reminder_dt = datetime.strptime(remind_at, "%Y-%m-%d %H:%M")
        event_dt = datetime.strptime(event_at, "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return remind_at, event_at

    lead = event_dt - reminder_dt
    if lead < timedelta(0) or lead > timedelta(days=7):
        lead = timedelta(0)
    target_date = current.date() + timedelta(days=days)
    corrected_event = datetime.combine(target_date, event_dt.time())
    corrected_reminder = corrected_event - lead
    return (
        corrected_reminder.strftime("%Y-%m-%d %H:%M"),
        corrected_event.strftime("%Y-%m-%d %H:%M"),
    )


def _normalize_leading_event_date(content: str, current_label: str) -> str:
    """只更新句首的事件日期，避免误改“讨论明天计划”等正文语义。"""
    text = re.sub(r"^(【[^】]*】\s*)+", "", content or "").strip()
    prefix_match = re.match(r"^(?P<prefix>(?:请|记得|提醒我)(?:在)?)?", text)
    prefix = prefix_match.group("prefix") or ""
    rest = text[len(prefix):]

    # “明早/今晚”是日期和时段的紧凑写法，替换日期时保留时段。
    compact = re.match(r"^(?:今|明)(?P<period>早|晚)", rest)
    if compact:
        period = "早上" if compact.group("period") == "早" else "晚上"
        return f"{prefix}{current_label}{period}{rest[compact.end():]}"

    relative = re.match(
        r"^(?:今天|今日|明天|明日|后天|大后天)"
        r"(?P<period>早上|上午|中午|下午|晚上|晚间|夜里)?",
        rest,
    )
    if relative:
        return f"{prefix}{current_label}{relative.group('period') or ''}{rest[relative.end():]}"

    dated = re.match(
        r"^(?:(?:本周|这周|下周|下下周|周|星期|礼拜)[一二三四五六日天]"
        r"|(?:\d{4}年)?\d{1,2}月\d{1,2}[日号])",
        rest,
    )
    if dated:
        return f"{prefix}{current_label}{rest[dated.end():]}"
    return text


def format_reminder_push_text(
    content: str,
    event_at: str = "",
    remind_at: str = "",
    *,
    now: datetime | None = None,
) -> str:
    """按推送当刻重新生成“今天/明天/具体日期”提醒文案。"""
    clean = re.sub(r"^(【[^】]*】\s*)+", "", content or "").strip()
    try:
        event_dt = datetime.strptime(event_at, "%Y-%m-%d %H:%M") if event_at else None
    except (TypeError, ValueError):
        event_dt = None
    if not event_dt:
        return f"【提醒】{clean}\n（{remind_at}）"

    current = now or datetime.now()
    delta = (event_dt.date() - current.date()).days
    weekday = _WEEKDAY_CN[event_dt.weekday()]
    time_str = event_dt.strftime("%H:%M")
    date_label = f"{event_dt.month}月{event_dt.day}日"

    if delta == 0:
        content_label = "今天"
        detail = f"今天（{weekday}）{time_str}"
        push_prefix = "【今日提醒】"
    elif delta == 1:
        content_label = "明天"
        detail = f"明天（{weekday}）{time_str}"
        push_prefix = "【明日提醒】"
    elif delta == 2:
        content_label = "后天"
        detail = f"后天（{weekday}，{date_label}）{time_str}"
        push_prefix = "【日程提醒】"
    elif delta > 2:
        content_label = date_label
        detail = f"{date_label}（{weekday}）{time_str}，还有 {delta} 天"
        push_prefix = "【日程提醒】"
    else:
        content_label = date_label
        detail = f"{date_label}（{weekday}）{time_str}"
        push_prefix = "【提醒】"

    normalized = _normalize_leading_event_date(clean, content_label)
    return f"{push_prefix}{normalized}\n（{detail}）"
