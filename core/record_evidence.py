"""Pure contracts for dbsheet field presence, query scope and page coverage.

An exhausted continuation page does not prove a complete query. Row coverage
does not establish that every field was returned. No I/O or model calls live here.
"""
from copy import deepcopy


def field_state(fields, name):
    if not isinstance(fields, dict) or name not in fields:
        return "not_returned"
    value = fields[name]
    return "empty" if value is None or value == "" or value == [] or value == {} else "value"


def record_scope(*, file_id=None, sheet_id=None, fields=None, filter=None,
                 view_id=None, start_token=None):
    return deepcopy({"file_id": file_id, "sheet_id": sheet_id, "fields": fields or None,
                     "filter": filter, "view_id": view_id, "start_token": start_token or ""})


def valid_page(page):
    return (isinstance(page, dict) and not page.get("error") and page.get("ok") is not False
            and page.get("code", 0) in (0, "0") and isinstance(page.get("records"), list)
            and all(isinstance(row, dict) and row.get("id")
                    and (row.get("fields") is None or isinstance(row["fields"], dict))
                    for row in page["records"]))


def continuation(page):
    token = page.get("page_token") if "page_token" in page else page.get("next_page_token")
    return token if isinstance(token, str) and token else None


def exhausted(page):
    return (valid_page(page) and not page.get("continuation_error") and not continuation(page)
            and not page.get("has_more")
            and page.get("page_exhausted", page.get("is_complete", True)) is not False)


def record_counts(records, *, complete, reported_total=None):
    identified = all(isinstance(row, dict) and row.get("id") for row in records)
    observed = len({str(row["id"]) for row in records}) if identified else None
    reported = type(reported_total) is int and reported_total >= 0
    conflict = reported and observed is not None and reported_total < observed
    known = not conflict and (reported or (complete and identified))
    return {"fetched": len(records), "total": (reported_total if reported else observed) if known else None,
            "total_is_known": bool(known),
            "total_basis": ("server_reported_query_total" if reported else "complete_query_unique_record_ids")
                           if known else "not_reported",
            **({"count_warning": "服务端总数小于已返回唯一记录数，总数未确认。"} if conflict else {})}


def normalize_page(page, *, scope):
    result = deepcopy(page) if isinstance(page, dict) else {}
    result["_record_scope"] = deepcopy(scope)
    if not valid_page(result):
        rows = result.get("records")
        return {**result, "error": result.get("error") or "记录接口未返回有效原始记录页。",
                "is_complete": False, "page_exhausted": False,
                **record_counts(rows if isinstance(rows, list) else [], complete=False)}
    token = continuation(result)
    raw_token = result.get("page_token") if "page_token" in result else result.get("next_page_token")
    more = bool(token or result.get("has_more"))
    done = exhausted(result)
    # A reported total larger than a head page requires continuation even
    # when the server omitted has_more. A tail page cannot make this inference.
    if (not scope.get("start_token") and type(result.get("total")) is int
            and result["total"] > len({str(row["id"]) for row in result["records"]})):
        more, done = True, False
    if raw_token and not isinstance(raw_token, str):
        more, done = True, False
    result.update(next_page_token=token, has_more=more, page_exhausted=done,
                  is_complete=done and not scope.get("start_token"), continuation_available=bool(token))
    result.update(record_counts(result["records"], complete=result["is_complete"], reported_total=page.get("total")))
    if more and not token:
        result["continuation_error"] = "记录页声明尚有数据，但未返回有效继续游标。"
    return result


def combine_record_pages(pages):
    """Combine only a contiguous, same-query chain of authorized raw pages."""
    if not isinstance(pages, list) or not 1 <= len(pages) <= 20:
        raise ValueError("请选择本轮1至20个连续记录页")
    records, identities = [], set()
    scope, previous, totals = None, None, set()
    for page in pages:
        if not valid_page(page):
            raise ValueError("不能合并失败或无有效记录身份的页")
        current = page.get("_record_scope")
        if not isinstance(current, dict) or not current.get("file_id") or current.get("sheet_id") is None:
            raise ValueError("记录页缺少可核对的源查询范围")
        if scope is None:
            scope = deepcopy(current)
        elif ({k: v for k, v in current.items() if k != "start_token"}
              != {k: v for k, v in scope.items() if k != "start_token"}
              or not continuation(previous)
              or current.get("start_token") != continuation(previous)):
            raise ValueError("只能按真实游标顺序合并同一文件、工作表、字段、筛选和视图的连续页")
        for row in page["records"]:
            key = str(row["id"])
            if key in identities:
                raise ValueError("页间出现重复记录身份，不能把重叠页当成连续新记录统计")
            identities.add(key)
            records.append(deepcopy(row))
        if page.get("total_basis") == "server_reported_query_total" and type(page.get("total")) is int:
            totals.add(page["total"])
        previous = page
    complete = not scope.get("start_token") and exhausted(previous) and len(totals) <= 1
    result = {
        "records": records, "_record_scope": scope, "source_pages": len(pages),
        "has_more": bool(previous.get("has_more") or continuation(previous)),
        "next_page_token": continuation(previous), "page_exhausted": exhausted(previous),
        "is_complete": complete,
        **record_counts(records, complete=complete, reported_total=next(iter(totals)) if len(totals) == 1 else None),
    }
    if len(totals) > 1:
        result["continuation_error"] = "读取期间源查询总数变化，不能确认完整快照"
    elif previous.get("continuation_error"):
        result["continuation_error"] = previous["continuation_error"]
    return result
