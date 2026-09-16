"""模型时间上下文必须提供可直接引用的北京时间与相对日期。"""

from datetime import datetime, timedelta, timezone

from agent.assistant import _beijing_calendar_context, build_system_prompt


def test_relative_calendar_uses_the_same_year_and_correct_weekdays():
    fixed = datetime(2026, 9, 16, 10, 30, tzinfo=timezone(timedelta(hours=8)))

    context = _beijing_calendar_context(fixed)

    assert "北京时间：2026年09月16日（周三） 10:30" in context
    assert "前天：2026年09月14日（周一）" in context
    assert "昨天：2026年09月15日（周二）" in context
    assert "今天：2026年09月16日（周三）" in context
    assert "明天：2026年09月17日（周四）" in context
    assert "禁止自行换算日期或星期" in context


def test_system_prompt_contains_authoritative_relative_calendar():
    fixed = datetime(2026, 9, 16, 10, 30, tzinfo=timezone(timedelta(hours=8)))

    prompt = build_system_prompt("测试用户", "staff", None, now=fixed)

    assert "## 当前时间与相对日期" in prompt
    assert "昨天：2026年09月15日（周二）" in prompt
    assert "明天：2026年09月17日（周四）" in prompt
