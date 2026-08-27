"""
WPS 多维表格 REST API 客户端
使用 v7 API + OAuth Bearer token
需要 KSO-1 签名的接口（视图、附件）自动附加签名头
所有写操作路径均经过实测验证
"""
import json
import hashlib
import hmac
import httpx
from datetime import datetime, timezone
from email.utils import formatdate
from auth.wps_oauth import get_app_id, get_app_secret, get_app_token

OPENAPI_HOST = "https://openapi.wps.cn"


# ── KSO-1 签名 ─────────────────────────────────────────────

def _kso1_sign(method: str, uri: str, content_type: str,
               kso_date: str, body_str: str) -> dict:
    """生成 KSO-1 签名头"""
    sha256_hex = ""
    if body_str:
        sha256_hex = hashlib.sha256(body_str.encode("utf-8")).hexdigest()
    mac = hmac.new(
        get_app_secret().encode("utf-8"),
        f"KSO-1{method}{uri}{content_type}{kso_date}{sha256_hex}".encode("utf-8"),
        hashlib.sha256,
    )
    authorization = f"KSO-1 {get_app_id()}:{mac.hexdigest()}"
    return {"X-Kso-Date": kso_date, "X-Kso-Authorization": authorization}


def _now_rfc1123() -> str:
    return formatdate(usegmt=True)


def _base_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


def _signed_headers(access_token: str, method: str, path: str, body_str: str) -> dict:
    kso_date = _now_rfc1123()
    headers = _base_headers(access_token)
    headers.update(_kso1_sign(method, path, "application/json", kso_date, body_str))
    return headers


# ── HTTP 基础方法 ───────────────────────────────────────────

async def _post(access_token: str, path: str, body: dict, signed: bool = False,
               extra_headers: dict = None) -> dict:
    import logging
    logger = logging.getLogger(__name__)
    body_str = json.dumps(body, ensure_ascii=False)
    url = f"{OPENAPI_HOST}{path}"
    headers = (_signed_headers(access_token, "POST", path, body_str)
               if signed else _base_headers(access_token))
    if extra_headers:
        headers.update(extra_headers)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, content=body_str.encode("utf-8"), headers=headers)
        if not resp.is_success:
            logger.error(f"[WPS API ERROR] POST {resp.status_code} {path}")
            logger.error(f"[WPS API ERROR] request body: {body_str}")
            logger.error(f"[WPS API ERROR] signed: {signed}")
            logger.error(f"[WPS API ERROR] response: {resp.text}")
            resp.raise_for_status()
        return resp.json()


async def _get(access_token: str, path: str, params: dict = None, signed: bool = False) -> dict:
    import logging
    logger = logging.getLogger(__name__)
    url = f"{OPENAPI_HOST}{path}"
    headers = (_signed_headers(access_token, "GET", path, "")
               if signed else _base_headers(access_token))
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers, params=params)
        if not resp.is_success:
            logger.error(f"[WPS API ERROR] GET {resp.status_code} {path}")
            logger.error(f"[WPS API ERROR] response: {resp.text}")
            resp.raise_for_status()
        return resp.json()


async def _put_raw(url: str, data: bytes, content_type: str = None,
                   extra_headers: dict = None) -> httpx.Response:
    """直接 PUT 到第三方存储（干净客户端，不复用全局 headers）
    content_type: 可选，若云存储未签名该 header 则不传
    extra_headers: 可选，云存储要求的额外头（如 Authorization）
    """
    import logging
    logger = logging.getLogger(__name__)
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    if extra_headers:
        headers.update(extra_headers)
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.put(url, content=data, headers=headers)
        if not resp.is_success:
            logger.error(f"[PUT_RAW] 失败 {resp.status_code} URL={url[:80]}")
            logger.error(f"[PUT_RAW] 响应头: {dict(resp.headers)}")
            logger.error(f"[PUT_RAW] 响应体: {resp.text[:500]}")
        resp.raise_for_status()
        return resp


def _parse_records(data: dict) -> dict:
    """把返回记录的 fields JSON 字符串解析为 dict"""
    for rec in data.get("records", []):
        if isinstance(rec.get("fields"), str):
            try:
                rec["fields"] = json.loads(rec["fields"])
            except Exception:
                pass
    return data


# ── Schema ─────────────────────────────────────────────────

async def get_schema(access_token: str, file_id: str) -> dict:
    """获取多维表格结构（工作表列表 + 字段信息）"""
    result = await _get(access_token, f"/v7/coop/dbsheet/{file_id}/schema")
    return result.get("data", result)


# ── 记录 ───────────────────────────────────────────────────

async def list_records(access_token: str, file_id: str, sheet_id: int,
                       page_size: int = 500, page_token: str = None,
                       fields: list = None, filter: dict = None,
                       view_id: str = None, max_records: int = None) -> dict:
    # 指定了 page_token（AI 手动翻页）或 max_records（限量查询）时，单次请求
    if page_token is not None or max_records is not None:
        body = {"page_size": min(page_size, 1000)}
        if page_token:   body["page_token"] = page_token
        if fields:       body["fields"] = fields
        if filter:       body["filter"] = filter
        if view_id:      body["view_id"] = view_id
        if max_records:  body["max_records"] = max_records
        result = await _post(access_token,
                             f"/v7/coop/dbsheet/{file_id}/sheets/{sheet_id}/records", body)
        parsed = _parse_records(result.get("data", result))
        parsed["fetched"] = len(parsed.get("records", []))
        parsed["has_more"] = bool(parsed.get("has_more", False))
        parsed["is_complete"] = not parsed["has_more"]
        parsed.setdefault("next_page_token", None)
        return parsed

    # 默认：自动翻页，拉取全部记录（上限 3000 条，防止超大表撑爆 token）
    MAX_AUTO_FETCH = 3000
    all_records = []
    current_token = None
    total = 0

    has_more = False
    while True:
        body = {"page_size": 1000}
        if current_token: body["page_token"] = current_token
        if fields:        body["fields"] = fields
        if filter:        body["filter"] = filter
        if view_id:       body["view_id"] = view_id
        result = await _post(access_token,
                             f"/v7/coop/dbsheet/{file_id}/sheets/{sheet_id}/records", body)
        parsed = _parse_records(result.get("data", result))
        batch = parsed.get("records", [])
        all_records.extend(batch)
        total = parsed.get("total", len(all_records))
        has_more = parsed.get("has_more", False)
        current_token = parsed.get("next_page_token")
        if not has_more or not current_token or len(all_records) >= MAX_AUTO_FETCH:
            break

    return {
        "records": all_records,
        "total": total,
        "has_more": bool(has_more),
        "fetched": len(all_records),
        "next_page_token": current_token if has_more else None,
        "is_complete": not bool(has_more),
        "auto_fetch_limit": MAX_AUTO_FETCH,
        "continuation_available": bool(has_more and current_token),
    }


async def create_records(access_token: str, file_id: str, sheet_id: int,
                         records: list) -> dict:
    """records: [{"字段名": "值", ...}, ...]"""
    # 过滤空记录，防止 AI 传入空 dict 导致 WPS 创建空行
    valid_records = [r for r in records if r]
    print(f"[CREATE RECORDS DEBUG] sheet_id={sheet_id}, records={json.dumps(valid_records, ensure_ascii=False)}")
    if not valid_records:
        return {"error": "records 列表为空或全部为空对象，未创建任何记录"}
    api_records = [{"fields_value": json.dumps(r, ensure_ascii=False)} for r in valid_records]
    result = await _post(access_token,
                         f"/v7/coop/dbsheet/{file_id}/sheets/{sheet_id}/records/create",
                         {"records": api_records})
    data = result.get("data", result)
    parsed = _parse_records(data)
    # 附加诊断信息，帮助 AI 明确告知用户操作位置
    parsed["_operated_on"] = {"file_id": file_id, "sheet_id": sheet_id}
    if not parsed.get("records"):
        # WPS 返回空 data（常见行为），提示 AI 验证并告知用户
        parsed["_verify_hint"] = (
            f"WPS 未返回新记录ID（data为空），请立即调用 list_records "
            f"(file_id={file_id}, sheet_id={sheet_id}) 并使用 filter 参数"
            f"只查刚创建的那条记录（按任务名称/标题字段过滤），禁止全量 list_records，"
            "并向用户明确报告创建在哪个工作表、创建了哪条记录。"
        )
    else:
        # WPS 返回了新记录ID，引导 AI 用 filter 精准验证，避免全量查询
        new_ids = [r.get("id") for r in parsed["records"] if r.get("id")]
        if new_ids:
            parsed["_verify_hint"] = (
                f"新记录ID：{new_ids}。如需验证，请用 filter 参数只查这些ID，"
                f"禁止全量 list_records 验证，节省 token。"
            )
    return parsed


async def update_records(access_token: str, file_id: str, sheet_id: int,
                         records: list) -> dict:
    """records: [{"id": "记录ID", "字段名": "新值", ...}, ...]"""
    api_records = []
    record_ids = []
    for r in records:
        rec_id = r.get("id") or r.get("record_id")
        fields = {k: v for k, v in r.items() if k not in ("id", "record_id")}
        print(f"[UPDATE RECORDS DEBUG] rec_id={rec_id}, fields={json.dumps(fields, ensure_ascii=False)}")
        api_records.append({"id": rec_id, "fields_value": json.dumps(fields, ensure_ascii=False)})
        record_ids.append(rec_id)
    result = await _post(access_token,
                         f"/v7/coop/dbsheet/{file_id}/sheets/{sheet_id}/records/update",
                         {"records": api_records})
    code = result.get("code", -1)
    if code == 0:
        # WPS update 同样可能静默返回 code:0, data:{}，无法区��"真正更新"和"字段格式错误被忽略"
        # 明确返回已提交的 ID 列表，AI 可据此判断；Contact/Link等字段值格式错误时WPS不报错
        return {
            "ok": True,
            "updated_ids": record_ids,
            "updated_count": len(record_ids),
            "message": (
                f"更新请求已提交，共 {len(record_ids)} 条：{record_ids}。"
                "注意：Contact（联系人）字段必须传 [{\"id\":\"wps_open_id\",\"nickName\":\"姓名\"}] 格式；"
                "如实际未更新请先调用 list_wps_contacts 获取正确的 open_id 再重试。"
            ),
        }
    return {"ok": False, "error": f"API 返回错误码 {code}", "detail": result}


async def delete_records(access_token: str, file_id: str, sheet_id: int,
                         record_ids: list) -> dict:
    result = await _post(access_token,
                         f"/v7/coop/dbsheet/{file_id}/sheets/{sheet_id}/records/batch_delete",
                         {"records": record_ids})
    code = result.get("code", -1)
    if code == 0:
        # WPS batch_delete 无论 ID 是否存在都返回 code=0，data={}
        # 明确告知已提交的 ID 列表，AI 可据此判断
        return {
            "ok": True,
            "deleted_ids": record_ids,
            "message": (
                f"删除请求已提交，共 {len(record_ids)} 条：{record_ids}。"
                "⚠️ WPS 无法区分「真正删除」和「ID不存在/sheet错误」，均返回code:0。"
                "必须立即调用 list_records 验证这些记录已不存在；"
                "若记录仍存在，说明ID或sheet_id有误，重新查询后再次删除。"
            ),
        }
    return {"ok": False, "error": f"API 返回错误码 {code}", "detail": result}


# ── 工作表 ─────────────────────────────────────────────────

async def create_sheet(access_token: str, file_id: str, name: str = None,
                       fields: list = None, views: list = None) -> dict:
    """fields 和 views 均为必填，name 选填"""
    body = {
        "fields": fields or [{"name": "名称", "type": "MultiLineText", "data": {"unique_value": False}}],
        "views":  views  or [{"name": "默认视图", "type": "Grid"}],
    }
    if name:
        body["name"] = name
    result = await _post(access_token, f"/v7/coop/dbsheet/{file_id}/sheets/create", body, signed=True)
    return result.get("data", result)


async def delete_sheet(access_token: str, file_id: str, sheet_id: int) -> dict:
    result = await _post(access_token,
                         f"/v7/coop/dbsheet/{file_id}/sheets/{sheet_id}/delete", {})
    return result.get("data", result)


# ── 字段 ───────────────────────────────────────────────────

async def create_fields(access_token: str, file_id: str, sheet_id: int,
                        fields: list) -> dict:
    result = await _post(access_token,
                         f"/v7/coop/dbsheet/{file_id}/sheets/{sheet_id}/fields",
                         {"fields": fields}, signed=True)
    return result.get("data", result)


async def update_fields(access_token: str, file_id: str, sheet_id: int,
                        fields: list) -> dict:
    result = await _post(access_token,
                         f"/v7/coop/dbsheet/{file_id}/sheets/{sheet_id}/fields/update",
                         {"fields": fields})
    return result.get("data", result)


async def delete_fields(access_token: str, file_id: str, sheet_id: int,
                        field_ids: list) -> dict:
    result = await _post(access_token,
                         f"/v7/coop/dbsheet/{file_id}/sheets/{sheet_id}/fields/delete",
                         {"field_ids": field_ids})
    return result.get("data", result)


# ── 视图（需要 KSO-1 签名）─────────────────────────────────

async def create_view(access_token: str, file_id: str, sheet_id: int,
                      name: str, view_type: str = "Grid") -> dict:
    """创建视图，view_type: Grid / Kanban / Gallery / Calendar / Form
    返回包含 view.id 的 dict
    """
    result = await _post(access_token,
                         f"/v7/coop/dbsheet/{file_id}/sheets/{sheet_id}/views",
                         {"name": name, "type": view_type}, signed=True)
    data = result.get("data", result)
    # 统一把 view.id 提升到顶层方便调用方取用
    if "view" in data and "id" not in data:
        data["id"] = data["view"]["id"]
    return data


async def delete_view(access_token: str, file_id: str, sheet_id: int,
                      view_id: str) -> dict:
    """删除视图"""
    result = await _post(access_token,
                         f"/v7/coop/dbsheet/{file_id}/sheets/{sheet_id}/views/{view_id}/delete",
                         {}, signed=True)
    return result.get("data", result)


# ── 附件上传（需要 KSO-1 签名）────────────────────────────

# ── 表单（路径为 /v7/dbsheet/，需要 KSO-1 签名）────────────

async def get_form_meta(access_token: str, file_id: str,
                        sheet_id: int, view_id: str) -> dict:
    """获取表单元数据（名称、描述等）"""
    path = f"/v7/dbsheet/{file_id}/sheets/{sheet_id}/forms/{view_id}/meta"
    result = await _get(access_token, path, signed=True)
    return result.get("data", result)


async def update_form_meta(access_token: str, file_id: str,
                           sheet_id: int, view_id: str,
                           name: str = None, description: str = None) -> dict:
    """更新表单元数据（名称、描述）"""
    body = {}
    if name:        body["name"] = name
    if description: body["description"] = description
    path = f"/v7/dbsheet/{file_id}/sheets/{sheet_id}/forms/{view_id}/meta"
    result = await _post(access_token, path, body, signed=True)
    return result.get("data", result)


async def list_form_fields(access_token: str, file_id: str,
                           sheet_id: int, view_id: str) -> dict:
    """列出表单的所有问题（字段）"""
    path = f"/v7/dbsheet/{file_id}/sheets/{sheet_id}/forms/{view_id}/fields"
    result = await _get(access_token, path, signed=True)
    return result.get("data", result)


async def update_form_field(access_token: str, file_id: str,
                            sheet_id: int, view_id: str, field_id: str,
                            title: str = None, description: str = None,
                            required: bool = None, pre_field_id: str = None) -> dict:
    """更新表单问题（标题、描述、是否必填、排序）"""
    body = {}
    if title is not None:        body["title"] = title
    if description is not None:  body["description"] = description
    if required is not None:     body["required"] = required
    if pre_field_id is not None: body["pre_field_id"] = pre_field_id
    path = f"/v7/dbsheet/{file_id}/sheets/{sheet_id}/forms/{view_id}/fields/{field_id}/update"
    result = await _post(access_token, path, body, signed=True)
    return result.get("data", result)


# ── 仪表盘（路径为 /v7/dbsheet/，需要 KSO-1 签名）──────────

async def list_dashboards(access_token: str, file_id: str) -> dict:
    """列出多维表格的所有仪表盘"""
    path = f"/v7/dbsheet/{file_id}/dashboards"
    result = await _get(access_token, path, signed=True)
    return result.get("data", result)


async def copy_dashboard(access_token: str, file_id: str,
                         dashboard_id: str, name: str) -> dict:
    """复制仪表盘，name 为新仪表盘名称（必填）"""
    path = f"/v7/dbsheet/{file_id}/dashboards/{dashboard_id}/copy"
    result = await _post(access_token, path, {"name": name}, signed=True)
    return result.get("data", result)


# ── 父子记录（需要 KSO-1 签名）────────────────────────────

async def get_parent_status(access_token: str, file_id: str,
                            sheet_id: int) -> dict:
    """查询工作表父子关系是否启用"""
    path = f"/v7/coop/dbsheet/{file_id}/sheets/{sheet_id}/records/parents/status"
    result = await _get(access_token, path, signed=True)
    return result.get("data", result)


async def enable_parent(access_token: str, file_id: str,
                        sheet_id: int) -> dict:
    """启用父子关系（仅对前端展示生效）"""
    path = f"/v7/coop/dbsheet/{file_id}/sheets/{sheet_id}/records/parents/enable"
    result = await _post(access_token, path, {}, signed=True)
    return result.get("data", result)


async def disable_parent(access_token: str, file_id: str,
                         sheet_id: int) -> dict:
    """禁用父子关系（仅对前端展示生效）"""
    path = f"/v7/coop/dbsheet/{file_id}/sheets/{sheet_id}/records/parents/disable"
    result = await _post(access_token, path, {}, signed=True)
    return result.get("data", result)


async def list_children(access_token: str, file_id: str,
                        sheet_id: int, parent_id: str,
                        page_size: int = 100, page_token: str = None) -> dict:
    """查询某条记录的子记录列表"""
    params = {"page_size": page_size}
    if page_token: params["page_token"] = page_token
    path = f"/v7/coop/dbsheet/{file_id}/sheets/{sheet_id}/records/parents/{parent_id}/children"
    result = await _get(access_token, path, params=params, signed=True)
    return _parse_records(result.get("data", result))


async def bind_children(access_token: str, file_id: str,
                        sheet_id: int, parent_id: str,
                        child_ids: list) -> dict:
    """绑定子记录到父记录"""
    path = f"/v7/coop/dbsheet/{file_id}/sheets/{sheet_id}/records/parents/{parent_id}/children/batch_bind"
    result = await _post(access_token, path, {"child_ids": child_ids}, signed=True)
    return result.get("data", result)


async def unbind_children(access_token: str, file_id: str,
                          sheet_id: int, parent_id: str,
                          child_ids: list) -> dict:
    """解绑子记录"""
    path = f"/v7/coop/dbsheet/{file_id}/sheets/{sheet_id}/records/parents/{parent_id}/children/batch_unbind"
    result = await _post(access_token, path, {"child_ids": child_ids}, signed=True)
    return result.get("data", result)


# ── Webhook 订阅（需要 KSO-1 签名）────────────────────────

async def list_hooks(access_token: str, file_id: str) -> dict:
    """查询文件的所有 webhook 订阅"""
    path = f"/v7/coop/dbsheet/{file_id}/hooks"
    result = await _get(access_token, path, signed=True)
    return result.get("data", result)


async def create_hook(access_token: str, file_id: str,
                      command: str, callback_url: str,
                      data: dict = None) -> dict:
    """创建 webhook 订阅
    command 可选值:
      create_record       — 新增记录
      update_sheet        — 修改记录
      remove_record       — 删除记录
      update_records_parent — 父子记录变动
      create_field        — 新增字段
      update_field        — 更新字段
      remove_field        — 删除字段
    """
    body = {
        "file_id": file_id,
        "command": command,
        "callback_url": callback_url,
    }
    if data:
        body["data"] = data
    path = f"/v7/coop/dbsheet/{file_id}/hooks/create"
    result = await _post(access_token, path, body, signed=True)
    return result.get("data", result)


async def delete_hook(access_token: str, file_id: str,
                      hook_id: str) -> dict:
    """取消 webhook 订阅"""
    path = f"/v7/coop/dbsheet/{file_id}/hooks/{hook_id}/delete"
    result = await _post(access_token, path,
                         {"file_id": file_id, "hook_id": hook_id}, signed=True)
    return result.get("data", result)


# ── 附件上传（需要 KSO-1 签名）────────────────────────────
async def send_wps_message(access_token: str, to_user_id: str,
                           text: str) -> dict:
    """通过WPS协作直接给用户发消息（点对点私信）
    to_user_id: 收件人的WPS openId（wps_tokens.wps_user_id）
    """
    path = "/v7/messages/create"
    body = {
        "type": "text",
        "receiver": {
            "receiver_id": to_user_id,
            "type": "user",
        },
        "content": {
            "type": "text",
            "text": text,
        },
    }
    result = await _post(access_token, path, body, signed=True)
    return result.get("data", result)


async def batch_send_wps_message(access_token: str, to_user_ids: list,
                                 text: str) -> dict:
    """批量向多个WPS用户发消息（异步接口，有轻微延迟）"""
    path = "/v7/messages/batch_create"
    body = {
        "type": "text",
        "receivers": [{"receiver_ids": to_user_ids, "type": "user"}],
        "content": {"type": "text", "text": text},
    }
    result = await _post(access_token, path, body, signed=True)
    return result.get("data", result)


async def create_record_comment(access_token: str, file_id: str,
                               sheet_id: int, record_id: str,
                               content: str, at_user_id: str = "") -> dict:
    """在记录下添加评论，支持 @某人
        content: 评论文字，如 "@张三 请尽快更新进度"
    at_user_id: WPS用户openId（有则附加at结构触发系统通知，无则纯文字评论）
    """
    path = f"/v7/coop/dbsheet/{file_id}/sheets/{sheet_id}/records/{record_id}/comments/create"
    if at_user_id:
        body = {
            "content": content,
            "at_users": [{"open_id": at_user_id}],
        }
    else:
        body = {"content": content}
    return await _post(access_token, path, body, signed=True)


async def upload_attachment(access_token: str, file_id: str,
                            file_name: str, file_data: bytes,
                            content_type: str = "application/octet-stream") -> dict:
    """上传附件（三步流程），返回可写入 Attachment 字段的值
    Step1: POST /v7/documents/{file_id}/attachments/upload/address  → upload_id, request(url/method/headers), send_back_params
    Step2: PUT/POST 到 Step1 返回的 url，带 Step1 返回的 headers，body 为文件内容
    Step3: POST /v7/documents/{file_id}/attachments/upload/complete → attachment_id
    写入记录: {"uploadId": attachment_id, "fileName": ..., "size": ..., "source": "Cloud", "type": ...}
    """
    # Step 1: 申请上传地址
    path1 = f"/v7/documents/{file_id}/attachments/upload/address"
    body1 = {"name": file_name, "size": len(file_data), "content_type": content_type}
    body1_str = json.dumps(body1)
    headers1 = _signed_headers(access_token, "POST", path1, body1_str)
    async with httpx.AsyncClient(timeout=60) as client:
        resp1 = await client.post(f"{OPENAPI_HOST}{path1}", content=body1_str, headers=headers1)
        if not resp1.is_success:
            print(f"[ATTACH] Step1 error {resp1.status_code}: {resp1.text}")
            resp1.raise_for_status()
        r1 = resp1.json()
    d1 = r1.get("data", {})
    upload_id      = d1.get("upload_id")
    req_info       = d1.get("request", {})
    send_back      = d1.get("send_back_params", {})
    upload_url     = req_info.get("url")
    upload_method  = req_info.get("method", "PUT").upper()
    upload_headers = req_info.get("headers", {})
    if not upload_id or not upload_url:
        raise ValueError(f"Step1 未返回 upload_id/url: {r1}")
    print(f"[ATTACH] Step1 ok, upload_id={upload_id}")

    # Step 2: 上传文件内容到云存储
    async with httpx.AsyncClient(timeout=120) as client:
        resp2 = await client.request(upload_method, upload_url,
                                     content=file_data, headers=upload_headers)
        if not resp2.is_success:
            print(f"[ATTACH] Step2 error {resp2.status_code}: {resp2.text}")
            resp2.raise_for_status()
        # etag/key 可能在响应头里，etag 标准格式含双引号需去除
        print(f"[ATTACH] Step2 response headers: {dict(resp2.headers)}")
        etag = resp2.headers.get("etag", send_back.get("etag", "")).strip('"')
        key  = resp2.headers.get("newfilename") or resp2.headers.get("key") or send_back.get("key", "")
    print(f"[ATTACH] Step2 ok, etag={etag}, key={key}")

    # Step 3: 提交上传完成
    path3 = f"/v7/documents/{file_id}/attachments/upload/complete"
    body3 = {"upload_id": upload_id, "params": {"etag": etag, "key": key}}
    body3_str = json.dumps(body3)
    headers3 = _signed_headers(access_token, "POST", path3, body3_str)
    async with httpx.AsyncClient(timeout=60) as client:
        resp3 = await client.post(f"{OPENAPI_HOST}{path3}", content=body3_str, headers=headers3)
        if not resp3.is_success:
            print(f"[ATTACH] Step3 error {resp3.status_code}: {resp3.text}")
            resp3.raise_for_status()
        r3 = resp3.json()
    print(f"[ATTACH] Step3 response: {r3}")
    attachment_id = r3.get("data", {}).get("attachment_id")
    kind = r3.get("data", {}).get("kind", "")
    if not attachment_id:
        raise ValueError(f"Step3 未返回 attachment_id: {r3}")
    print(f"[ATTACH] Step3 ok, attachment_id={attachment_id}, kind={kind}")
    # 提取文件扩展名（不带点号）
    ext = file_name.split('.')[-1].lower() if '.' in file_name else ""
    return {
        "uploadId": attachment_id,  # WPS 需要 uploadId
        "fileName": file_name,      # WPS 需要 fileName（不是 name）
        "size": len(file_data),
        "source": "upload_ks3",
        "type": ext,
    }


# ── 网盘上传（kso.file.readwrite，需要用户 token + KSO-1 签名）──

async def get_file_meta(access_token: str, file_id: str) -> dict:
    """获取文件元数据，含 drive_id / parent_id"""
    result = await _get(access_token, f"/v7/files/{file_id}/meta", signed=True)
    return result.get("data", result)


async def get_drive_children(access_token: str, drive_id: str, parent_id: str,
                              page_size: int = 10) -> dict:
    """获取网盘目录下的子文件列表
    parent_id=0 表示根目录。响应 items 中每个条目的 id 字段为该条目自身 ID。
    """
    path = f"/v7/drives/{drive_id}/files/{parent_id}/children"
    result = await _get(access_token, path, params={"page_size": page_size}, signed=True)
    return result.get("data", result)


async def create_drive_folder(access_token: str, drive_id: str, parent_id: str,
                               name: str) -> dict:
    """在网盘目录下新建文件夹，返回文件夹信息（含 data.id）
    parent_id=0 表示根目录（此 API 接受 "0"）
    """
    body = {"file_type": "folder", "name": name, "on_name_conflict": "rename"}
    result = await _post(access_token,
                         f"/v7/drives/{drive_id}/files/{parent_id}/create", body, signed=True)
    return result.get("data", result)


async def create_dbsheet(access_token: str, name: str, ref_file_id: str) -> dict:
    """在网盘根目录新建多维表格，返回 {file_id, file_name}
    ref_file_id: 用于获取 drive_id，传用户任意一个已有表格的 file_id
    """
    meta = await get_file_meta(access_token, ref_file_id)
    drive_id = meta.get("drive_id")
    if not drive_id:
        return {"error": f"无法获取网盘信息，请确认 WPS 已连接。meta={meta}"}
    body = {"file_type": "dbsheet", "name": name}
    result = await _post(access_token,
                         f"/v7/drives/{drive_id}/files/0/create", body, signed=True)
    data = result.get("data", result)
    new_file_id = data.get("id") or data.get("file_id")
    if not new_file_id:
        return {"error": f"创建失败，API 返回：{data}"}
    return {"ok": True, "file_id": new_file_id, "file_name": name,
            "message": f"多维表格《{name}》创建成功（file_id: {new_file_id}），已自动添加到您的表格列表。"}


# 缓存已创建/找到的 AI附件 文件夹 ID，避免每次上传都创建新文件夹
# key: drive_id, value: folder_id
_drive_folder_cache: dict = {}


async def _drive_upload_apply(access_token: str, drive_id: str, parent_id: str,
                               name: str, size: int, sha256: str,
                               content_type: str = "application/octet-stream") -> dict:
    """申请网盘直传地址（第一步）
    WPS 公网必传字段：name + size + hashes（md5 或 sha256 至少一种）
    ⚠️ 注意：hashes 是数组对象格式，不是顶层 sha1/sha256 字段
    ⚠️ 需要 KSO-1 签名
    """
    body = {
        "name": name,
        "size": size,
        "hashes": [{"sum": sha256, "type": "sha256"}],
    }
    path = f"/v7/drives/{drive_id}/files/{parent_id}/request_upload"
    print(f"[DRIVE UPLOAD] request_upload path={path} body={json.dumps(body, ensure_ascii=False)}")
    result = await _post(access_token, path, body, signed=True)
    return result.get("data", result)


async def _drive_upload_commit(access_token: str, drive_id: str, parent_id: str,
                                upload_id: str) -> dict:
    """提交网盘上传完成（第三步），返回文件信息
    与其他网盘 API 一致，使用 KSO-1 签名
    """
    body = {"upload_id": upload_id}
    result = await _post(access_token,
                         f"/v7/drives/{drive_id}/files/{parent_id}/commit_upload", body, signed=True)
    return result.get("data", result)


async def upload_to_drive(access_token: str, dbsheet_file_id: str,
                           file_name: str, file_data: bytes,
                           content_type: str = "application/octet-stream") -> dict:
    """
    将文件上传到 WPS 网盘（与多维表格同目录），返回可写入 Attachment 字段的云文档格式：
    {"name": "...", "mode": "cloud", "type": "docx", "uploadId": "网盘file_id", "size": N}

    流程：
    1. get_file_meta → drive_id, parent_id
    2. drive_upload_apply → upload_id, upload_url
    3. PUT 文件二进制到直传地址（不带 WPS Token）
    4. drive_upload_commit → cloud file_id
    """
    from pathlib import Path as _Path

    # Step 1: 获取多维表格所在的 drive_id 和 parent_id
    print(f"[DRIVE UPLOAD] get_file_meta file_id={dbsheet_file_id}")
    meta = await get_file_meta(access_token, dbsheet_file_id)
    print(f"[DRIVE UPLOAD] meta={str(meta)[:500]}")
    drive_id  = meta.get("drive_id")
    parent_id = str(meta.get("parent_id") or meta.get("parentId") or "0")
    if not drive_id:
        raise ValueError(f"无法获取 drive_id，file meta 返回: {meta}")

    # request_upload 接口不接受 parent_id="0"（根目录别名）
    # 需要获取或创建一个真实的文件夹 ID
    if parent_id == "0":
        # 使用缓存的文件夹 ID
        if drive_id in _drive_folder_cache:
            parent_id = _drive_folder_cache[drive_id]
            print(f"[DRIVE UPLOAD] using cached folder_id={parent_id}")
        else:
            # 先尝试从根目录子列表找「AI附件」文件夹
            print(f"[DRIVE UPLOAD] parent_id=0, looking for 'AI附件' folder in root")
            children = await get_drive_children(access_token, drive_id, "0", page_size=50)
            items = children.get("items", [])
            print(f"[DRIVE UPLOAD] root children count={len(items)}, names={[i.get('name') for i in items[:10]]}")
            folder_id = None
            exact_id  = None   # 精确匹配 "AI附件"
            fuzzy_id  = None   # 模糊匹配 "AI附件(N)"
            for item in items:
                item_name = item.get("name", "")
                item_type = item.get("file_type") or item.get("type") or ""
                if item_type != "folder":
                    continue
                if item_name == "AI附件":
                    exact_id = str(item.get("id"))
                    break   # 精确匹配，立即停止
                elif item_name.startswith("AI附件(") and not fuzzy_id:
                    fuzzy_id = str(item.get("id"))
            folder_id = exact_id or fuzzy_id
            if folder_id:
                chosen_name = "AI附件" if exact_id else "AI附件(N)"
                print(f"[DRIVE UPLOAD] found existing '{chosen_name}' folder id={folder_id}")
            if not folder_id:
                # 创建「AI附件」文件夹
                print(f"[DRIVE UPLOAD] creating 'AI附件' folder in root")
                folder_result = await create_drive_folder(access_token, drive_id, "0", "AI附件")
                folder_id = str(folder_result.get("id") or folder_result.get("file_id") or "")
                print(f"[DRIVE UPLOAD] created folder: {folder_result}")
            if not folder_id:
                raise ValueError(f"无法获取或创建 AI附件 文件夹: {folder_result}")
            _drive_folder_cache[drive_id] = folder_id
            parent_id = folder_id
            print(f"[DRIVE UPLOAD] using folder_id={parent_id}")

    # Step 2: 申请上传（hashes 数组格式，公网必传 sha256）
    import hashlib as _hashlib
    file_sha256 = _hashlib.sha256(file_data).hexdigest()
    print(f"[DRIVE UPLOAD] apply drive_id={drive_id} parent_id={parent_id} name={file_name} size={len(file_data)} sha256={file_sha256}")
    apply = await _drive_upload_apply(access_token, drive_id, parent_id, file_name, len(file_data),
                                       sha256=file_sha256, content_type=content_type)
    print(f"[DRIVE UPLOAD] apply result={str(apply)[:300]}")
    upload_id  = apply.get("upload_id")
    store_req  = apply.get("store_request", {})
    upload_url = store_req.get("url")
    if not upload_id or not upload_url:
        raise ValueError(f"申请上传失败，未返回 upload_id/store_request.url: {apply}")

    # Step 3: 直传文件到底层存储
    # ⚠️ ksc-bj.ag.wps.cn 是 WPS 金山云存储，非标准预签名 URL，可能需要 WPS token 认证
    print(f"[DRIVE UPLOAD] PUT to storage url={upload_url[:80]}...")
    print(f"[DRIVE UPLOAD] store_request 完整内容: {store_req}")
    # 尝试带 Authorization header（如果 ksc 存储需要 WPS token）
    auth_headers = {"Authorization": f"Bearer {access_token}"}
    await _put_raw(upload_url, file_data, extra_headers=auth_headers)

    # Step 4: 提交完成
    print(f"[DRIVE UPLOAD] commit upload_id={upload_id}")
    commit = await _drive_upload_commit(access_token, drive_id, parent_id, upload_id)
    print(f"[DRIVE UPLOAD] commit result={str(commit)[:300]}")
    cloud_file_id = commit.get("id")
    if not cloud_file_id:
        raise ValueError(f"提交完成失败，未返回 id: {commit}")

    ext = _Path(file_name).suffix.lstrip(".").lower() or "bin"
    print(f"[DRIVE UPLOAD] ok cloud_file_id={cloud_file_id} ext={ext}")
    return {
        "name":     file_name,
        "mode":     "cloud",
        "type":     ext,
        "uploadId": cloud_file_id,
        "size":     len(file_data),
    }

# ── 传统表格（kso.sheets.readwrite，需要 KSO-1 签名）──────────

async def sheets_list_worksheets(access_token: str, file_id: str) -> dict:
    """获取传统表格所有工作表列表"""
    result = await _get(access_token, f"/v7/sheets/{file_id}/worksheets", signed=True)
    return result.get("data", result)


async def sheets_create_worksheet(access_token: str, file_id: str,
                                   name: str = None, position: dict = None) -> dict:
    """创建工作表，position 默认插到末尾"""
    body = {"position": position or {"end": True}}
    if name:
        body["name"] = name
    result = await _post(access_token, f"/v7/sheets/{file_id}/worksheets", body, signed=True)
    return result.get("data", result)


async def sheets_update_worksheet(access_token: str, file_id: str,
                                   worksheet_id: int, name: str = None,
                                   move_sheet_id: int = None,
                                   move_type: str = None) -> dict:
    """重命名或移动工作表"""
    body = {}
    if name:           body["name"] = name
    if move_sheet_id:  body["move_sheet_id"] = move_sheet_id
    if move_type:      body["move_type"] = move_type
    result = await _post(access_token,
                         f"/v7/sheets/{file_id}/worksheets/{worksheet_id}/update",
                         body, signed=True)
    return {"ok": result.get("code", -1) == 0, "code": result.get("code")}


async def sheets_delete_worksheets(access_token: str, file_id: str,
                                    worksheet_ids: list) -> dict:
    """批量删除工作表"""
    result = await _post(access_token,
                         f"/v7/sheets/{file_id}/worksheets/batch_delete",
                         {"worksheet_ids": worksheet_ids}, signed=True)
    return {"ok": result.get("code", -1) == 0, "code": result.get("code")}


async def sheets_copy_worksheet(access_token: str, file_id: str,
                                 worksheet_id: int,
                                 copy_first_sheet: bool = False) -> dict:
    """复制工作表"""
    result = await _post(access_token,
                         f"/v7/sheets/{file_id}/worksheets/{worksheet_id}/copy",
                         {"copy_first_sheet": copy_first_sheet}, signed=True)
    return result.get("data", result)


async def sheets_get_range(access_token: str, file_id: str, worksheet_id: int,
                            row_from: int, row_to: int,
                            col_from: int, col_to: int) -> dict:
    """读取区域数据，坐标从0开始"""
    params = {
        "row_from": row_from, "row_to": row_to,
        "col_from": col_from, "col_to": col_to,
    }
    path = f"/v7/sheets/{file_id}/worksheets/{worksheet_id}/range_data"
    result = await _get(access_token, path, params=params, signed=True)
    return result.get("data", result)


async def sheets_update_range(access_token: str, file_id: str, worksheet_id: int,
                               range_data: list) -> dict:
    """写入/更新区域数据。
    range_data 每项格式：
    {
      "row_from": 0, "row_to": 0, "col_from": 0, "col_to": 0,
      "op_type": "value",   # 写文本值
      "formula": "内容或公式",                   # 公式用 =SUM(A1:A3)，普通值直接写
    }
    op_type 可选：value（文本）/ formula（公式）
    """
    # 规范化 op_type 值：WPS API 只接受 cell_operation_type_formula
    normalized_data = []
    for item in range_data:
        normalized_item = item.copy()
        normalized_item["op_type"] = "cell_operation_type_formula"
        normalized_data.append(normalized_item)

    result = await _post(access_token,
                         f"/v7/sheets/{file_id}/worksheets/{worksheet_id}/range_data/batch_update",
                         {"range_data": normalized_data}, signed=True)
    return result.get("data", result)


async def sheets_delete_range(access_token: str, file_id: str, worksheet_id: int,
                               range_data: list, shift_type: str = "shift_up") -> dict:
    """删除区域数据并移动单元格
    shift_type: shift_up（上移）/ shift_left（左移）
    """
    result = await _post(access_token,
                         f"/v7/sheets/{file_id}/worksheets/{worksheet_id}/range_data/batch_delete",
                         {"range_data": range_data, "shift_type": shift_type}, signed=True)
    return {"ok": result.get("code", -1) == 0, "code": result.get("code")}


async def sheets_create_range(access_token: str, file_id: str, worksheet_id: int,
                               range_data: list) -> dict:
    """插入行数据（在指定行插入新行）
    range_data 每项格式：{"col": 0, "op_type": "cell_operation_type_value", "formula": "值"}
    """
    result = await _post(access_token,
                         f"/v7/sheets/{file_id}/worksheets/{worksheet_id}/rows",
                         {"range_data": range_data}, signed=True)
    return {"ok": result.get("code", -1) == 0, "code": result.get("code")}


async def sheets_find_range(access_token: str, file_id: str, worksheet_id: int,
                             range_: dict, filter_: dict,
                             page: dict = None, show_total: bool = False) -> dict:
    """查找/筛选区域数据
    range_: {"row_from":0,"row_to":100,"col_from":0,"col_to":10}
    filter_: {"search":[{"col":0,"value":["关键词"]}]}
    """
    body = {"range": range_, "filter": filter_, "show_total": show_total}
    if page:
        body["page"] = page
    result = await _post(access_token,
                         f"/v7/sheets/{file_id}/worksheets/{worksheet_id}/range_data/find",
                         body, signed=True)
    return result.get("data", result)


async def sheets_create_file(access_token: str, name: str, ref_file_id: str) -> dict:
    """在网盘根目录新建传统表格文件（.xlsx），返回 {file_id, file_name}
    ref_file_id: 用于获取 drive_id，传用户任意一个已有文件的 file_id
    """
    meta = await get_file_meta(access_token, ref_file_id)
    drive_id = meta.get("drive_id")
    if not drive_id:
        return {"error": f"无法获取网盘信息，请确认 WPS 已连接。meta={meta}"}
    body = {"file_type": "sheet", "name": name}
    result = await _post(access_token,
                         f"/v7/drives/{drive_id}/files/0/create", body, signed=True)
    data = result.get("data", result)
    new_file_id = data.get("id") or data.get("file_id")
    if not new_file_id:
        return {"error": f"创建失败，API 返回：{data}"}
    return {"ok": True, "file_id": new_file_id, "file_name": name,
            "message": f"传统表格《{name}》创建成功（file_id: {new_file_id}）"}


# ── 通讯录（kso.contact.read，需要 App Token + KSO-1 签名）─

async def list_contacts(access_token: str = None, dept_id: str = None,
                        page_size: int = 100, page_token: str = None) -> dict:
    """查询企业下所有用户列表（官方文档：GET /v7/users），自动翻页取全量
    使用 App Token + KSO-1 签名，access_token 参数保留但不使用（改用 app token）
    返回 {"users": [{"id": "用户id", "name": "张三", "email": "..."}], "total": N}
    id 字段优先使用数字格式的 account_id（Contact 字段需要），无则回退字母格式 user_id
    """
    from auth.db import get_wps_account_id_map
    # {字母user_id: 数字account_id} 映射，用字母id做key查数字id
    account_id_map = get_wps_account_id_map()

    app_token = await get_app_token()
    all_users = []
    cur_token = page_token
    while True:
        params = {"status": "active", "page_size": page_size, "with_total": "true"}
        if cur_token:
            params["page_token"] = cur_token
        result = await _get(app_token, "/v7/users", params=params, signed=True)
        data = result.get("data", result)
        for u in data.get("items", []):
            letter_id = u.get("id", "")
            name = u.get("user_name", "")
            # 优先用数据库里手动维护的数字 account_id，其次 API 返回的 account_id，最后回退字母 id
            numeric_id = (account_id_map.get(letter_id, "")
                          or u.get("account_id", "")
                          or letter_id)
            all_users.append({
                "id": numeric_id,
                "user_id": letter_id,
                "name": name,
                "email": u.get("email", ""),
                "title": u.get("title", ""),
                "status": u.get("status", ""),
            })
        cur_token = data.get("next_page_token", "")
        if not cur_token:
            break
    return {
        "users": all_users,
        "total": len(all_users),
    }


# ── App Token 消息（kso.chat_message.readwrite，app token）─


async def send_bot_message(wps_user_id: str, text: str) -> dict:
    """以应用机器人身份给指定 WPS 用户发私信
    wps_user_id: 收件人的 WPS open_id（从 list_contacts 的 id 字段获取）
    使用 App Token + KSO-1 签名，type=text，content.text.type=plain
    """
    try:
        app_token = await get_app_token()
        body = {
            "type": "text",
            "receiver": {
                "receiver_id": wps_user_id,
                "type": "user",
            },
            "content": {
                "text": {
                    "content": text,
                    "type": "plain",
                },
            },
        }
        result = await _post(app_token, "/v7/messages/create", body, signed=True)
        print(f"[WPS BOT MSG] send to {wps_user_id} => {str(result)[:200]}")
        code = result.get("code", -1)
        if code == 0:
            return {"ok": True}
        return {"ok": False, "error": f"API 返回错误码 {code}", "detail": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}
