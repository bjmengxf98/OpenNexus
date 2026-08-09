"""部门驾驶舱：从 WPS 多维表格聚合数据并保存 HTML 前端所需快照。"""

from __future__ import annotations

import json
import re
import asyncio
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from openai import AsyncOpenAI

from agent.wps_client import get_schema, list_records
from auth import db
from auth.wps_oauth import auto_refresh_token_for_user, is_token_expiring_soon


VALID_VIEWS = {"overview", "daily", "tasks", "projects"}
COLORS = ["#58a6ff", "#79c0ff", "#f0b72f", "#3fb950", "#db61a2", "#a78bfa", "#39c5cf", "#f97316"]


class DashboardError(RuntimeError):
    pass


def _norm(value: Any) -> str:
    return re.sub(r"[\s_\-—·/（）()]+", "", str(value or "")).lower()


def _plain(value: Any, link_lookup: dict[str, str] | None = None) -> str:
    """把 WPS Contact/Link/Select 等复杂字段转成展示文本。"""
    link_lookup = link_lookup or {}
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped in link_lookup:
            return link_lookup[stripped]
        if stripped.startswith(("[", "{")):
            try:
                return _plain(json.loads(stripped), link_lookup)
            except Exception:
                pass
        return stripped
    if isinstance(value, list):
        parts = [_plain(item, link_lookup) for item in value]
        return "、".join(dict.fromkeys(part for part in parts if part))
    if isinstance(value, dict):
        for key in ("nickName", "nickname", "name", "text", "label", "title", "display_name"):
            if value.get(key):
                return _plain(value[key], link_lookup)
        for key in ("value", "values", "id", "record_id"):
            if key in value:
                return _plain(value[key], link_lookup)
        return "、".join(_plain(item, link_lookup) for item in value.values() if _plain(item, link_lookup))
    return str(value)


def _record_fields(record: dict) -> dict:
    fields = record.get("fields") or record.get("fields_value") or {}
    if isinstance(fields, str):
        try:
            fields = json.loads(fields)
        except Exception:
            fields = {}
    return fields if isinstance(fields, dict) else {}


def _field(fields: dict, aliases: tuple[str, ...], link_lookup: dict[str, str] | None = None) -> str:
    normalized = {_norm(key): value for key, value in fields.items()}
    for alias in aliases:
        key = _norm(alias)
        if key in normalized:
            return _plain(normalized[key], link_lookup)
    for alias in aliases:
        key = _norm(alias)
        if len(key) < 2:
            continue
        for field_key, value in normalized.items():
            if key in field_key or field_key in key:
                return _plain(value, link_lookup)
    return ""


def _date_value(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        for key in ("value", "date", "timestamp", "text"):
            if key in value:
                parsed = _date_value(value[key])
                if parsed:
                    return parsed
        return None
    if isinstance(value, list):
        return _date_value(value[0]) if value else None
    if isinstance(value, (int, float)):
        number = float(value)
        try:
            if number > 10_000_000_000:
                return datetime.fromtimestamp(number / 1000).date()
            if number > 1_000_000_000:
                return datetime.fromtimestamp(number).date()
        except Exception:
            return None
    text = _plain(value)
    if not text:
        return None
    text = text.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
    match = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except Exception:
        return None


def _field_date(fields: dict, aliases: tuple[str, ...]) -> date | None:
    normalized = {_norm(key): value for key, value in fields.items()}
    for alias in aliases:
        key = _norm(alias)
        if key in normalized:
            parsed = _date_value(normalized[key])
            if parsed:
                return parsed
    for alias in aliases:
        key = _norm(alias)
        for field_key, value in normalized.items():
            if key in field_key:
                parsed = _date_value(value)
                if parsed:
                    return parsed
    return None


def _schema_sheets(schema: dict) -> list[dict]:
    if not isinstance(schema, dict):
        return []
    for key in ("sheets", "sheet_list", "worksheets"):
        value = schema.get(key)
        if isinstance(value, list):
            return value
    data = schema.get("data")
    return _schema_sheets(data) if isinstance(data, dict) else []


def _sheet_name(sheet: dict) -> str:
    return str(sheet.get("name") or sheet.get("title") or sheet.get("sheet_name") or "")


def _sheet_id(sheet: dict) -> Any:
    return sheet.get("id") or sheet.get("sheet_id") or sheet.get("sheetId")


def _pick_sheet(sheets: list[dict], kind: str) -> dict | None:
    scored: list[tuple[int, dict]] = []
    for sheet in sheets:
        name = _sheet_name(sheet)
        normalized = _norm(name)
        score = 0
        if kind == "daily":
            if "每日进展" in name:
                score = 100
            elif "进展" in name:
                score = 60
        elif kind == "tasks":
            if normalized in {"任务", "任务表", "部门任务"}:
                score = 100
            elif "任务" in name and not any(word in name for word in ("每日", "进展", "子任务", "模板")):
                score = 65
        elif kind == "projects":
            if normalized in {"项目", "项目表", "部门项目"}:
                score = 100
            elif "项目" in name and not any(word in name for word in ("类型", "模板", "看板")):
                score = 65
        elif kind == "people":
            if any(word in name for word in ("人员信息", "部门人员", "成员", "通讯录")):
                score = 80
        if score:
            scored.append((score, sheet))
    return max(scored, key=lambda item: item[0])[1] if scored else None


async def _access_token(user_id: int) -> str:
    token = db.get_wps_token(user_id)
    if not token or not token.get("access_token"):
        raise DashboardError("尚未连接 WPS，请先在主页完成授权")
    expires_at = token.get("expires_at") or "2000-01-01"
    if is_token_expiring_soon(expires_at, minutes=5):
        if not await auto_refresh_token_for_user(user_id):
            raise DashboardError("WPS 授权已过期，请返回主页重新连接 WPS")
        token = db.get_wps_token(user_id)
    return token.get("access_token", "")


async def _load_records(
    token: str,
    file_id: str,
    sheet: dict | None,
    target_date: date | None = None,
) -> list[dict]:
    if not sheet or not _sheet_id(sheet):
        return []
    if target_date:
        # 日报表通常远超500行，而部分 WPS 表不返回分页游标；必须在服务端按日期筛选。
        date_filter = {
            "mode": "AND",
            "criteria": [{
                "field": "填报日期",
                "operator": "Equals",
                "values": [target_date.strftime("%Y/%m/%d")],
            }],
        }
        try:
            result = await list_records(
                token, file_id, _sheet_id(sheet), page_size=500,
                max_records=1000, filter=date_filter,
            )
            records = result.get("records", []) if isinstance(result, dict) else []
            if records:
                return records
        except Exception as exc:
            print(f"[DASHBOARD] WPS date filter fallback: {exc}")
    result = await list_records(token, file_id, _sheet_id(sheet))
    return result.get("records", []) if isinstance(result, dict) else []


def _people_lookup(records: list[dict]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for record in records:
        fields = _record_fields(record)
        name = _field(fields, ("姓名", "人员姓名", "成员姓名", "名称"))
        rid = str(record.get("id") or "")
        if rid and name:
            lookup[rid] = name
    return lookup


def _distribution(title: str, counter: Counter) -> dict:
    items = []
    for index, (label, value) in enumerate(counter.most_common(12)):
        items.append({"label": label or "未注明", "value": value, "color": COLORS[index % len(COLORS)]})
    return {"title": title, "items": items}


def _kpi(label: str, value: Any, unit: str = "", tone: str = "blue") -> dict:
    return {"label": label, "value": value, "unit": unit, "tone": tone}


def _extract_metrics(texts: list[str]) -> list[dict]:
    totals: defaultdict[tuple[str, str], float] = defaultdict(float)
    pattern = re.compile(r"([\u4e00-\u9fa5A-Za-z0-9 /_-]{2,18}?)\s*(\d[\d,]*(?:\.\d+)?)\s*(万|万元|次|条|份|人|场|项|小时|张|个)")
    for text in texts:
        for match in pattern.finditer(text or ""):
            label = re.sub(r"^[，。；、：:\s]+", "", match.group(1)).strip()[-14:]
            label_lower = label.lower()
            if "pdf" in label_lower and "jpg" in label_lower:
                label = "PDF 转 JPG"
            elif "档号章" in label:
                label = "加档号章"
            elif "重命名" in label:
                label = "批量重命名"
            elif "请款" in label:
                label = "科研预算请款"
            elif "截图" in label:
                label = "行程单截图"
            if not label:
                continue
            number = float(match.group(2).replace(",", ""))
            totals[(label, match.group(3))] += number
    metrics = []
    for (label, unit), value in sorted(totals.items(), key=lambda item: item[1], reverse=True)[:6]:
        metrics.append({"label": label, "value": int(value) if value.is_integer() else round(value, 1), "unit": unit})
    return metrics


def _business_categories(texts: list[str]) -> list[str]:
    joined = "\n".join(texts).lower()
    rules = [
        ("图档与文件处理", ("图档", "pdf", "文件", "重命名", "截图", "盖章")),
        ("会议与外部沟通", ("会议", "汇报", "报送", "沟通", "协调")),
        ("科研与专家工作", ("科研", "课题", "专家", "技术", "试验")),
        ("财务与行政事务", ("预算", "请款", "报销", "用印", "审批", "行程单")),
        ("项目与任务推进", ("项目", "任务", "进度", "计划", "交付")),
        ("智能化应用", ("workbuddy", "智能体", "自动化", "ai", "批量处理")),
    ]
    return [label for label, words in rules if any(word in joined for word in words)]


def _daily_report(items: list[dict], selected_count: int, filled: int, blank: int, people: Counter) -> dict:
    valid = [item for item in items if not item.get("empty")]
    all_lines = [line for item in valid for line in item.get("lines", [])]
    categories = _business_categories(all_lines)
    category_text = " + ".join(categories[:5]) if categories else "多项日常业务"
    overview = (
        f"当日共读取 {selected_count} 条进展记录，{len(people)} 人提交了 {filled} 条有实质内容的工作进展"
        f"{f'，另有 {blank} 条空白记录需要补充' if blank else ''}。"
        f"部门工作呈现“{category_text}”并行推进的特点。"
    )
    grouped: dict[str, dict] = {}
    for item in valid:
        person = item.get("title") or "未注明"
        group = grouped.setdefault(person, {"name": person, "tag": "综合工作", "items": [], "count": 0})
        group["count"] += 1
        if item.get("subtitle") and group["tag"] == "综合工作":
            group["tag"] = item["subtitle"]
        group["items"].extend(item.get("lines", []))
    people_detail = sorted(grouped.values(), key=lambda row: (-row["count"], row["name"]))
    for person in people_detail:
        person_categories = _business_categories(person["items"])
        if person_categories:
            person["tag"] = " / ".join(person_categories[:2])
    metrics = _extract_metrics(all_lines)
    highlights = [
        {"title": f"识别到重点工作量：{m['value']}{m['unit']}", "body": m["label"], "tag": "数据亮点"}
        for m in sorted(metrics, key=lambda row: float(row.get("value") or 0), reverse=True)[:3]
    ]
    for person in people_detail:
        text = "；".join(person["items"])
        if re.search(r"workbuddy|智能体|自动化|批量", text, re.I):
            highlights.append({"title": f"{person['name']}开展智能化应用", "body": text[:260], "tag": "提效亮点"})
    followups = []
    follow_pattern = re.compile(r"未成功|未完成|失败|问题|风险|困难|待跟进|待处理|需协调|下一步|阻塞|延期")
    for item in items:
        for line in item.get("lines", []):
            if follow_pattern.search(line):
                followups.append({"title": f"{item.get('title') or '未注明'} · 待跟进", "body": line})
    if blank:
        followups.append({"title": f"{blank} 条空白记录", "body": "仅填写了日期或缺少具体工作内容，建议补填或清理，避免影响后续分析。"})
    recommendations = []
    if highlights:
        recommendations.append({"title": "沉淀高价值工作方法", "body": "把高工作量、可复用的处理方法整理为部门模板或标准操作流程，供其他成员复用。"})
    if followups:
        recommendations.append({"title": "形成待办闭环", "body": "为未成功、存在风险或需要协调的事项明确责任人和下一次检查时间。"})
    if blank:
        recommendations.append({"title": "提升填报质量", "body": "减少空白和笼统记录，尽量写明动作、成果、数量和下一步，提升自动分析准确度。"})
    if not recommendations:
        recommendations.append({"title": "保持固定复盘节奏", "body": "建议持续按日记录、按周复盘，用连续数据观察工作负荷和项目推进趋势。"})
    return {
        "overview": overview, "categories": categories, "highlights": highlights[:6],
        "people": people_detail, "followups": followups[:12],
        "recommendations": recommendations, "source": "rules",
    }


def _work_report(payload: dict, noun: str) -> dict:
    values = {item["label"]: item["value"] for item in payload.get("kpis", [])}
    total, active = values.get(f"{noun}总数", 0), values.get("进行/待办", 0)
    completed, overdue = values.get("已完成", 0), values.get("已逾期", 0)
    blocked = values.get("阻塞/暂停", 0)
    items = payload.get("sections", [{}])[0].get("items", [])
    overview = f"当前共纳入 {total} 个{noun}，其中 {active} 个正在推进或待办，{completed} 个已完成。"
    if overdue or blocked:
        overview += f"需要重点关注 {overdue} 个逾期{noun}和 {blocked} 个阻塞或暂停{noun}。"
    highlights = [
        {"title": item.get("name", "未命名"), "body": f"负责人：{item.get('owner') or '未注明'}；当前状态：{item.get('status') or '未注明'}", "tag": "已完成"}
        for item in items if _status_kind(item.get("status", "")) == "completed"
    ][:6]
    followups = [
        {"title": item.get("name", "未命名"), "body": f"{item.get('status') or '待跟进'}；负责人：{item.get('owner') or '未注明'}；计划日期：{item.get('deadline') or '未填写'}"}
        for item in items if item.get("overdue") or _status_kind(item.get("status", "")) == "blocked"
    ][:15]
    recommendations = []
    if overdue:
        recommendations.append({"title": "优先处理逾期事项", "body": f"逐项确认 {overdue} 个逾期{noun}的真实进度、卡点和新的完成日期。"})
    if blocked:
        recommendations.append({"title": "解除阻塞", "body": f"对 {blocked} 个阻塞或暂停{noun}明确所需资源与协调人。"})
    if not recommendations:
        recommendations.append({"title": "保持节奏", "body": f"持续维护{noun}状态、负责人和计划日期，确保驾驶舱结论可靠。"})
    return {"overview": overview, "highlights": highlights, "people": [], "followups": followups, "recommendations": recommendations, "source": "rules"}


def _json_from_model(text: str) -> dict | None:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.I)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
        return value if isinstance(value, dict) else None
    except Exception:
        return None


async def _enrich_report_with_llm(user_id: int, payload: dict) -> dict:
    """用用户当前模型润色报告；失败时保留规则报告，不影响页面。"""
    report = payload.get("report") or {}
    report["ai_status"] = "running"
    cfg = db.get_llm_key(user_id) or {}
    if not cfg.get("api_key") or not cfg.get("model"):
        report["ai_status"] = "failed"
        report["ai_error"] = "尚未配置可用的大模型，请先到设置中完成配置"
        return payload
    compact = {
        "view": payload.get("view"), "date": payload.get("date"),
        "kpis": payload.get("kpis", []), "metrics": payload.get("metrics", []),
        "records": [
            {key: item.get(key) for key in ("title", "subtitle", "lines", "name", "status", "owner", "deadline", "priority", "parent", "overdue") if item.get(key) not in (None, "", [])}
            for section in payload.get("sections", []) for item in section.get("items", [])[:80]
        ],
        "rule_report": report,
    }
    prompt = f"""你是部门负责人身边的高级业务分析秘书。请根据下面的真实数据，写出正式工作情况分析报告的结构化总结。
要求：只能使用输入数据，绝不虚构；overview 写120—220字总体概况，归纳工作主线和特征，不能只重复数字；highlights 提炼2—6项成果或效率亮点，每项含 title、body、tag；people 按人员归并工作，每项含 name、tag、items 字符串数组，非人员日报可为空；followups 提取未成功、逾期、阻塞、风险和待协调项，每项含 title、body；recommendations 给出2—5条具体可执行建议，每项含 title、body；不要空洞表扬，不要 Markdown。只返回合法 JSON，对象键必须是 overview、highlights、people、followups、recommendations。
    数据：{json.dumps(compact, ensure_ascii=False)[:24000]}"""
    try:
        client = AsyncOpenAI(api_key=cfg["api_key"], base_url=cfg.get("base_url") or None, timeout=35, max_retries=0)
        actual_model = cfg["model"]
        enable_reasoning = False
        if actual_model.lower().endswith("-reasoning"):
            enable_reasoning = True
            actual_model = actual_model[:-len("-reasoning")]
        request_kwargs = {
            "model": actual_model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 8000 if enable_reasoning else 4000,
            "response_format": {"type": "json_object"},
        }
        # 与主聊天保持一致：-reasoning 是界面别名，不是接口模型名。
        if cfg.get("provider") == "deepseek" and actual_model in {"deepseek-v4-flash", "deepseek-v4-pro"}:
            request_kwargs["extra_body"] = {
                "thinking": {"type": "enabled" if enable_reasoning else "disabled"}
            }
            if enable_reasoning:
                request_kwargs["reasoning_effort"] = "max"
        else:
            request_kwargs["temperature"] = 0.25
        response = await asyncio.wait_for(
            client.chat.completions.create(**request_kwargs), timeout=40,
        )
        first_choice = response.choices[0]
        first_text = first_choice.message.content or ""
        parsed = _json_from_model(first_text)
        # JSON 模式偶尔仍会返回空内容，思考输出也可能耗尽额度导致最终 JSON 截断。
        # 自动用非思考模式重试一次；不记录原始业务内容，避免日志泄露 WPS 数据。
        if not parsed:
            first_finish = getattr(first_choice, "finish_reason", None) or "unknown"
            print(
                "[DASHBOARD] AI JSON parse failed, retrying without thinking: "
                f"content_chars={len(first_text)} finish_reason={first_finish}"
            )
            retry_kwargs = dict(request_kwargs)
            retry_kwargs["max_tokens"] = 5000
            retry_kwargs.pop("reasoning_effort", None)
            if cfg.get("provider") == "deepseek" and actual_model in {"deepseek-v4-flash", "deepseek-v4-pro"}:
                retry_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            retry_response = await asyncio.wait_for(
                client.chat.completions.create(**retry_kwargs), timeout=40,
            )
            retry_choice = retry_response.choices[0]
            retry_text = retry_choice.message.content or ""
            parsed = _json_from_model(retry_text)
        if parsed:
            for key in ("overview", "highlights", "people", "followups", "recommendations"):
                if parsed.get(key):
                    report[key] = parsed[key]
            report["source"] = "ai"
            report["ai_status"] = "success"
            report.pop("ai_error", None)
        else:
            report["ai_status"] = "failed"
            retry_finish = getattr(retry_choice, "finish_reason", None) or "unknown"
            report["ai_error"] = (
                "模型两次返回的结构化内容均无法解析，已保留原分析"
                f"（重试输出 {len(retry_text)} 字符，结束原因：{retry_finish}）"
            )
    except Exception as exc:
        print(f"[DASHBOARD] AI report fallback to rules: {type(exc).__name__}: {exc}")
        report["ai_status"] = "failed"
        report["ai_error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    payload["report"] = report
    return payload


DATE_ALIASES = ("填报日期", "进展日期", "日期", "工作日期", "创建日期", "创建时间")
PERSON_ALIASES = ("填报人", "人员", "姓名", "执行人", "任务执行人", "负责人", "项目负责人", "创建人")


def _daily_payload(records: list[dict], target: date, source: dict, lookup: dict[str, str]) -> dict:
    selected = []
    for record in records:
        fields = _record_fields(record)
        if _field_date(fields, DATE_ALIASES) == target:
            selected.append((record, fields))

    people = Counter()
    filled = 0
    blank = 0
    items = []
    metric_texts = []
    for record, fields in selected:
        person = _field(fields, PERSON_ALIASES, lookup) or "未注明"
        content = _field(fields, ("进展内容", "今日动作", "工作内容", "当日工作", "内容"), lookup)
        result = _field(fields, ("今日成果", "工作成果", "成果", "完成情况", "产出"), lookup)
        issue = _field(fields, ("问题/下一步", "问题与下一步", "存在问题", "下一步", "后续计划"), lookup)
        task = _field(fields, ("关联任务", "所属任务", "任务名称"), lookup)
        has_content = bool(content.strip() or result.strip())
        if has_content:
            filled += 1
            people[person] += 1
        else:
            blank += 1
        metric_texts.extend([content, result])
        items.append({
            "id": record.get("id", ""),
            "title": person,
            "subtitle": task,
            "lines": [line for line in (content, result and f"成果：{result}", issue and f"下一步：{issue}") if line],
            "empty": not has_content,
        })

    top_person = people.most_common(1)[0] if people else None
    insights = [f"当日共读取 {len(selected)} 条进展，其中 {filled} 条包含实质内容。"]
    if top_person:
        insights.append(f"填报最活跃的是 {top_person[0]}，共有 {top_person[1]} 条有效进展。")
    if blank:
        insights.append(f"发现 {blank} 条空白或缺少进展内容的记录，建议补充或清理。")
    if not selected:
        insights = ["该日期没有匹配到每日进展记录。"]

    payload = {
        "view": "daily",
        "title": f"{source.get('file_name') or '部门'} · {target.strftime('%Y/%m/%d')} 工作情况分析",
        "date": target.isoformat(),
        "source": source,
        "kpis": [
            _kpi("当日进展记录", len(selected), "条", "blue"),
            _kpi("有内容记录", filled, "条", "green"),
            _kpi("有内容填报人", len(people), "人", "gold"),
            _kpi("待补空白记录", blank, "条", "pink"),
        ],
        "metrics": _extract_metrics(metric_texts),
        "distributions": [_distribution("填报记录分布", people)],
        "insights": insights,
        "sections": [{"title": "当日进展明细", "type": "cards", "items": items}],
    }
    payload["report"] = _daily_report(items, len(selected), filled, blank, people)
    return payload


def _status_kind(status: str) -> str:
    text = _norm(status)
    if any(word in text for word in ("已完成", "完成", "办结", "关闭", "已验收")):
        return "completed"
    if any(word in text for word in ("阻塞", "暂停", "搁置", "风险")):
        return "blocked"
    if any(word in text for word in ("未开始", "待开始", "待办", "计划中")):
        return "pending"
    return "active"


def _work_payload(records: list[dict], kind: str, target: date, source: dict, lookup: dict[str, str]) -> dict:
    is_task = kind == "tasks"
    status_counter = Counter()
    owner_counter = Counter()
    items = []
    completed = active = blocked = overdue = 0
    for record in records:
        fields = _record_fields(record)
        name = _field(
            fields,
            ("任务名称", "名称", "标题") if is_task else ("项目名称", "名称", "标题"),
            lookup,
        ) or f"未命名{'任务' if is_task else '项目'}"
        status = _field(fields, ("当前状态", "任务状态", "项目状态", "状态"), lookup) or "未注明"
        owner = _field(
            fields,
            ("任务执行人", "执行人", "负责人", "责任人") if is_task else ("项目负责人", "负责人", "责任人"),
            lookup,
        ) or "未注明"
        deadline = _field_date(fields, ("计划完成日期", "完成日期", "截止日期", "计划结束日期", "结束日期"))
        priority = _field(fields, ("优先级", "重要程度", "紧急程度"), lookup)
        parent = _field(fields, ("所属项目", "关联项目"), lookup) if is_task else _field(fields, ("项目类型", "类型"), lookup)
        progress = _field(fields, ("完成进度", "进度", "完成率"), lookup)
        state = _status_kind(status)
        if state == "completed":
            completed += 1
        else:
            active += 1
        if state == "blocked":
            blocked += 1
        is_overdue = bool(deadline and deadline < target and state != "completed")
        if is_overdue:
            overdue += 1
        status_counter[status] += 1
        owner_counter[owner] += 1
        items.append({
            "id": record.get("id", ""),
            "name": name,
            "status": status,
            "owner": owner,
            "deadline": deadline.isoformat() if deadline else "",
            "priority": priority,
            "parent": parent,
            "progress": progress,
            "overdue": is_overdue,
        })

    items.sort(key=lambda item: (not item["overdue"], item["deadline"] or "9999", item["name"]))
    noun = "任务" if is_task else "项目"
    insights = [f"当前共 {len(records)} 个{noun}，其中 {active} 个未完成、{completed} 个已完成。"]
    if overdue:
        insights.append(f"有 {overdue} 个{noun}已超过计划日期，需要优先跟进。")
    if blocked:
        insights.append(f"有 {blocked} 个{noun}处于阻塞或暂停状态。")
    if not records:
        insights = [f"未找到{noun}工作表或工作表中暂无记录。"]

    payload = {
        "view": kind,
        "title": f"{noun}分析",
        "date": target.isoformat(),
        "source": source,
        "kpis": [
            _kpi(f"{noun}总数", len(records), "个", "blue"),
            _kpi("进行/待办", active, "个", "gold"),
            _kpi("已完成", completed, "个", "green"),
            _kpi("已逾期", overdue, "个", "red"),
            _kpi("阻塞/暂停", blocked, "个", "pink"),
        ],
        "metrics": [],
        "distributions": [
            _distribution(f"{noun}状态分布", status_counter),
            _distribution(f"{noun}负责人分布", owner_counter),
        ],
        "insights": insights,
        "sections": [{
            "title": f"{noun}明细",
            "type": "table",
            "columns": [
                {"key": "name", "label": f"{noun}名称"},
                {"key": "status", "label": "状态"},
                {"key": "owner", "label": "负责人"},
                {"key": "deadline", "label": "计划日期"},
                {"key": "priority", "label": "优先级/类型"},
                {"key": "parent", "label": "所属项目" if is_task else "项目类型"},
            ],
            "items": items[:500],
        }],
    }
    payload["report"] = _work_report(payload, noun)
    return payload


def _overview_payload(daily: dict, tasks: dict, projects: dict, target: date, source: dict) -> dict:
    def value(payload: dict, label: str) -> int:
        for item in payload.get("kpis", []):
            if item.get("label") == label:
                try:
                    return int(item.get("value", 0))
                except Exception:
                    return 0
        return 0

    overdue_tasks = [item for item in tasks.get("sections", [{}])[0].get("items", []) if item.get("overdue")]
    active_projects = [
        item for item in projects.get("sections", [{}])[0].get("items", [])
        if _status_kind(item.get("status", "")) != "completed"
    ]
    payload = {
        "view": "overview",
        "title": "部门整体驾驶舱",
        "date": target.isoformat(),
        "source": source,
        "kpis": [
            _kpi("项目总数", value(projects, "项目总数"), "个", "blue"),
            _kpi("进行中项目", value(projects, "进行/待办"), "个", "cyan"),
            _kpi("任务总数", value(tasks, "任务总数"), "个", "gold"),
            _kpi("逾期任务", value(tasks, "已逾期"), "个", "red"),
            _kpi("今日填报人", value(daily, "有内容填报人"), "人", "green"),
            _kpi("今日有效进展", value(daily, "有内容记录"), "条", "pink"),
        ],
        "metrics": daily.get("metrics", []),
        "distributions": (tasks.get("distributions", [])[:1] + projects.get("distributions", [])[:1]),
        "insights": daily.get("insights", []) + tasks.get("insights", [])[:1] + projects.get("insights", [])[:1],
        "sections": [
            {"title": "今日进展摘要", "type": "cards", "items": daily.get("sections", [{}])[0].get("items", [])[:12]},
            {
                "title": "需要关注的逾期任务",
                "type": "table",
                "columns": tasks.get("sections", [{}])[0].get("columns", []),
                "items": overdue_tasks[:20],
            },
            {
                "title": "进行中的项目",
                "type": "table",
                "columns": projects.get("sections", [{}])[0].get("columns", []),
                "items": active_projects[:20],
            },
        ],
    }
    daily_report = daily.get("report", {})
    task_report = tasks.get("report", {})
    project_report = projects.get("report", {})
    payload["report"] = {
        "overview": (
            f"{daily_report.get('overview', '')} "
            f"从持续推进情况看，{task_report.get('overview', '')} {project_report.get('overview', '')}"
        ).strip(),
        "highlights": (daily_report.get("highlights", []) + task_report.get("highlights", []) + project_report.get("highlights", []))[:8],
        "people": daily_report.get("people", []),
        "followups": (task_report.get("followups", []) + project_report.get("followups", []) + daily_report.get("followups", []))[:15],
        "recommendations": (task_report.get("recommendations", []) + project_report.get("recommendations", []) + daily_report.get("recommendations", []))[:6],
        "source": "rules",
    }
    return payload


def _cache_fresh(snapshot: dict | None, target: date, max_age_seconds: int = 300) -> bool:
    if not snapshot or not snapshot.get("report"):
        return False
    if target < date.today():
        return True
    try:
        generated = datetime.strptime(snapshot.get("generated_at", ""), "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - generated).total_seconds() < max_age_seconds
    except Exception:
        return False


async def generate_dashboard(
    user_id: int,
    file_id: str,
    view_type: str,
    snapshot_date: str | None = None,
    force: bool = False,
    use_ai: bool = False,
) -> dict:
    """生成或读取驾驶舱快照。"""
    if view_type not in VALID_VIEWS:
        raise DashboardError("不支持的驾驶舱页面")
    try:
        target = datetime.strptime(snapshot_date or date.today().isoformat(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise DashboardError("日期格式必须为 YYYY-MM-DD") from exc

    cached = db.get_dashboard_snapshot(user_id, file_id, view_type, target.isoformat())
    if not force and _cache_fresh(cached, target):
        cached["cached"] = True
        return cached

    files = db.list_wps_files(user_id)
    file_row = next((item for item in files if item.get("file_id") == file_id), None)
    if not file_row:
        raise DashboardError("该 WPS 文件未配置或无权访问")

    required = {"people"}
    if view_type == "overview":
        required.update(("daily", "tasks", "projects"))
    else:
        required.add(view_type)
    missing = [kind for kind in required if not db.get_dashboard_data_cache(user_id, file_id, kind)]
    if missing:
        # 仅首次使用会等待一次全量预热；后续所有页面和日期均只读 SQLite。
        try:
            from core.dashboard_cache import sync_dashboard_cache
            await sync_dashboard_cache(user_id, file_id, full=True, target_dates=[target])
        except Exception as exc:
            raise DashboardError(f"首次同步 WPS 数据失败：{exc}") from exc

    people_cache = db.get_dashboard_data_cache(user_id, file_id, "people") or {}
    lookup = _people_lookup(people_cache.get("records", []))
    source_base = {"file_id": file_id, "file_name": file_row.get("file_name") or file_id}

    def build(kind: str) -> dict:
        cache = db.get_dashboard_data_cache(user_id, file_id, kind) or {}
        records = cache.get("records", [])
        source = {
            **source_base,
            "sheet_name": cache.get("sheet_name") or "未找到",
            "synced_at": cache.get("synced_at") or "",
        }
        if kind == "daily":
            return _daily_payload(records, target, source, lookup)
        return _work_payload(records, kind, target, source, lookup)

    try:
        if view_type == "daily":
            payload = build("daily")
        elif view_type == "tasks":
            payload = build("tasks")
        elif view_type == "projects":
            payload = build("projects")
        else:
            daily = build("daily")
            tasks = build("tasks")
            projects = build("projects")
            payload = _overview_payload(daily, tasks, projects, target, source_base)
    except DashboardError:
        raise
    except Exception as exc:
        raise DashboardError(f"读取或聚合本地驾驶舱数据失败：{exc}") from exc

    if use_ai:
        payload = await _enrich_report_with_llm(user_id, payload)
    payload["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload["cached"] = False
    db.save_dashboard_snapshot(user_id, file_id, view_type, target.isoformat(), payload)
    return payload


async def generate_daily_for_all_users(target: date | None = None) -> dict:
    """为所有已启用且配置默认 WPS 文件的用户生成每日快照。"""
    target = target or date.today()
    result = {"ok": 0, "failed": 0, "errors": []}
    for user in db.list_users():
        if not user.get("is_enabled"):
            continue
        default_file = db.get_default_wps_file(user["id"])
        if not default_file:
            continue
        try:
            await generate_dashboard(
                user["id"], default_file["file_id"], "daily", target.isoformat(), force=True
            )
            result["ok"] += 1
        except Exception as exc:
            result["failed"] += 1
            result["errors"].append(f"user={user['id']}: {exc}")
    return result
