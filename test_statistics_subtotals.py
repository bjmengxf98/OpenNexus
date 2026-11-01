"""Locally computed subtotals, percentages and bounded output evidence."""
from copy import deepcopy
import json
import pytest

from agent.record_analyzer import FetchReceipt, analyze_record_snapshot
from agent.tool_planner import (
    _compact_tool_receipt, compact_seen_record_windows, compact_messages_for_retry,
)
from agent.assistant import build_system_prompt


def analyze(rows, **kwargs):
    return analyze_record_snapshot(FetchReceipt(rows, 1, False), kwargs)


def sample():
    states = ["doing", "done", "doing", "done", "done", "doing", "doing", "new", "done", "doing"]
    return [{"id": str(n), "fields": {"owner": "B" if n == 0 else "C" if n == 9 else "A", "state": value}}
            for n, value in enumerate(states)]


def test_group_subtotals_compute_conditional_counts_and_percentages():
    rows = sample();before = deepcopy(rows)
    result = analyze(rows, fields=["owner", "state"], group_by=["owner", "state"])
    group = next(row for row in result["group_subtotals"] if row["group"]["owner"] == "A")
    assert group["count"] == 8 and group["percent_of_records"] == 80
    stats = group["field_statistics"]["state"]
    assert stats["counted_records"] == 8 and stats["top_values_complete"]
    assert {r["value"]: (r["count"], r["percent_of_records"]) for r in stats["top_values"]} == {
        "done": (4, 50), "doing": (3, 37.5), "new": (1, 12.5),
    }
    assert result["_statistics_scope"]["counted_records"] == 10
    assert result["source_is_complete"] is False and rows == before


def test_subtotals_keep_missing_empty_zero_and_false_distinct():
    rows = [{"id": str(i), "fields": {"site": "a", **value}} for i,value in enumerate([
        {}, {"check": None}, {"check": 0}, {"check": False},
    ])]
    result = analyze(rows, group_by=["site", "check"])
    stat = result["group_subtotals"][0]["field_statistics"]["check"]
    assert (stat["not_returned"],stat["empty"],stat["non_empty"]) == (1,1,2)
    assert {v["value"]:v["percent_of_records"] for v in stat["top_values"]} == {"0":25,"否":25}


def test_numeric_subtotal_uses_actual_group_values_and_zeros():
    rows = [{"id": str(i),"fields":{"category":"x","phase":str(i),"hours":value}}
            for i,value in enumerate([0,3,False])]
    result = analyze(rows, group_by=["category","phase"], numeric_fields=["hours"])
    subtotal = result["group_subtotals"][0]
    assert subtotal["numeric"]["hours"] == {"sum":3.0,"count":2,"average":1.5}


def test_truncated_groups_have_explicit_coverage_and_are_not_complete():
    rows = [{"id":str(i),"fields":{"bucket":f"group-{i}","status":"x"}} for i in range(250)]
    result = analyze_record_snapshot(FetchReceipt(rows,1,True), {"group_by":["bucket","status"]})
    scope = result["_statistics_scope"]
    assert scope["group_count"] == 250 and scope["returned_group_count"] <= 200
    assert scope["group_statistics_complete"] is False
    assert scope["subtotal_group_count"] == 250 and scope["group_subtotals_complete"] is False
    assert result["is_complete"] is False and "未完整" in result["_note"]
    assert "均已完整返回" not in result["_note"]


def test_large_subtotal_output_omits_whole_groups_within_budget():
    rows = [{"id":str(i),"fields":{"group":str(i),"label":"值"*500+str(i)}} for i in range(120)]
    result = analyze(rows, group_by=["group","label"], max_chars=4000)
    assert len(json.dumps(result["group_subtotals"], ensure_ascii=False, separators=(",", ":"))) <= 2000
    assert result["_statistics_scope"]["group_subtotals_complete"] is False
    assert all("counted_records" in r["field_statistics"]["label"] for r in result["group_subtotals"])


def test_compaction_keeps_statistics_coverage_without_inventing_missing_subtotals():
    result = analyze(sample(),group_by=["owner","state"])
    folded = json.loads(_compact_tool_receipt(json.dumps(result,ensure_ascii=False)))
    assert folded["_statistics_scope"] == result["_statistics_scope"]
    result["_cached_analysis"] = {"receipt_index": 0, "arguments": {"group_by": ["owner", "state"]}}
    messages, count = compact_seen_record_windows([
        {"role": "tool", "content": json.dumps(result, ensure_ascii=False)},
    ], preserve_recent_tool_messages=0)
    assert count == 1
    assert json.loads(messages[0]["content"])["_statistics_scope"] == result["_statistics_scope"]


def test_subtotal_combines_different_explicit_empty_shapes_in_one_group():
    rows = [{"id": str(i), "fields": {"owner": owner, "state": "done"}}
            for i, owner in enumerate([None, "", [], {}])]
    result = analyze(rows, group_by=["owner", "state"])
    assert len(result["group_subtotals"]) == 1
    subtotal = result["group_subtotals"][0]
    assert subtotal["group_field_states"]["owner"] == "empty"
    assert subtotal["count"] == 4
    assert subtotal["field_statistics"]["state"]["top_values"][0]["count"] == 4


def test_subtotals_disclose_top_value_limit_and_local_filter_denominator():
    rows = [{"id": str(i), "fields": {"owner": "A" if i < 25 else "B", "state": str(i)}}
            for i in range(30)]
    result = analyze(rows, group_by=["owner", "state"],
                     local_filters=[{"field": "owner", "operator": "eq", "value": "A"}])
    subtotal = result["group_subtotals"][0]
    assert result["matched_records"] == 25 and subtotal["count"] == 25
    assert subtotal["percent_of_records"] == 100
    stat = subtotal["field_statistics"]["state"]
    assert stat["unique"] == 25 and not stat["top_values_complete"]
    assert stat["counted_records"] == 25 and len(stat["top_values"]) == 20
    assert all(item["percent_of_records"] == 4 for item in stat["top_values"])


def test_prompt_requires_computed_derived_counts_or_omission_without_business_router():
    prompt = build_system_prompt("tester","staff",None)
    assert "没有核算依据时" in prompt and "不添加未核算的细分数字" in prompt
    assert "先回复" not in prompt.split("## 【首要规则】")[0]


def fold_statistics(result, path):
    content = json.dumps(result, ensure_ascii=False)
    if path == "receipt":
        return json.loads(_compact_tool_receipt(content))
    messages = [{"role": "system", "content": "test"},
                {"role": "user", "content": "summarize"},
                {"role": "tool", "tool_call_id": "analysis", "content": content}]
    if path == "seen":
        messages, _ = compact_seen_record_windows(messages, preserve_recent_tool_messages=0)
    else:
        messages, _ = compact_messages_for_retry(messages)
    return json.loads(messages[-1]["content"])


@pytest.mark.parametrize("path", ["receipt", "seen", "retry"])
def test_fold_keeps_computed_subtotals_after_later_lookup_without_rows(path):
    result = analyze(sample(), fields=["owner", "state"], group_by=["owner", "state"], include_rows=True)
    result["_cached_analysis"] = {"receipt_indexes": [0, 1], "arguments": {"group_by": ["owner", "state"]}}
    before = deepcopy(result)
    folded = fold_statistics(result, path)
    assert folded["group_subtotals"] == result["group_subtotals"]
    assert folded["field_statistics"] == result["field_statistics"]
    assert folded["group_statistics"] == result["group_statistics"]
    assert folded["_statistics_scope"] == result["_statistics_scope"]
    assert folded["_analysis_scope"] == result["_analysis_scope"]
    assert folded["source_is_complete"] is False
    assert "rows" not in folded and "columns" not in folded
    assert "保留的统计" in folded["_instruction"]
    assert result == before


@pytest.mark.parametrize("path", ["receipt", "seen", "retry"])
def test_fold_statistics_budget_keeps_whole_groups_and_reports_omissions(path):
    rows = [{"id": str(i), "fields": {"bucket": f"group-{i:03}", "state": "done", "memo": "long-"*600}}
            for i in range(150)]
    result = analyze(rows, group_by=["bucket", "state"], fields=["memo"])
    result["_cached_analysis"] = {"receipt_index": 0, "arguments": {"group_by": ["bucket", "state"]}}
    before = deepcopy(result)
    folded = fold_statistics(result, path)
    retained = {k: folded[k] for k in ("field_statistics", "group_statistics", "group_subtotals", "_statistics_scope") if k in folded}
    assert len(json.dumps(retained, ensure_ascii=False, separators=(",", ":"))) <= 8000
    scope = folded["_statistics_scope"]
    assert not scope["group_subtotals_complete"]
    assert scope["returned_subtotal_group_count"] == len(folded["group_subtotals"])
    assert 0 < len(folded["group_subtotals"]) < len(result["group_subtotals"])
    assert scope["counted_records"] == 150
    assert not scope["group_statistics_complete"]
    assert scope["returned_group_count"] == len(folded["group_statistics"])
    assert all(item in result["group_subtotals"] for item in folded["group_subtotals"])
    assert result == before
    again = fold_statistics(folded, "receipt")
    assert {k: again[k] for k in retained} == retained


def test_fold_preserves_incomplete_statistics_without_turning_absence_into_zero():
    rows = [{"id": str(i), "fields": {"bucket": str(i), "state": "done"}} for i in range(250)]
    result = analyze(rows, group_by=["bucket", "state"], max_chars=4000)
    folded = fold_statistics(result, "receipt")
    assert not folded["_statistics_scope"]["group_statistics_complete"]
    assert not folded["_statistics_scope"]["group_subtotals_complete"]
    assert folded["_statistics_scope"]["group_count"] == 250
    assert folded["source_is_complete"] is False
    # An old already-folded receipt has coverage but no computed facts to reconstruct.
    legacy = {"_statistics_scope": result["_statistics_scope"], "matched_records": 250}
    restored = fold_statistics(legacy, "receipt")
    assert not any(key in restored for key in ("field_statistics", "group_statistics", "group_subtotals"))


def test_fold_keeps_statistics_from_different_scopes_in_their_own_receipts():
    first = analyze(sample(), group_by=["owner", "state"])
    second = analyze(sample()[:1], group_by=["owner", "state"])
    for index, result in enumerate((first, second)):
        result["_cached_analysis"] = {"receipt_index": index, "arguments": {}}
        result["_record_scope"] = {"file_id": "f", "sheet_id": index}
    messages = [{"role": "tool", "tool_call_id": str(i), "content": json.dumps(r)}
                for i, r in enumerate((first, second))]
    before = deepcopy(messages)
    folded, _ = compact_seen_record_windows(messages, preserve_recent_tool_messages=0)
    for index, msg in enumerate(folded):
        result = json.loads(msg["content"])
        assert result["_record_scope"]["sheet_id"] == index
        assert result["_statistics_scope"]["counted_records"] == (10 if index == 0 else 1)
        assert result["group_subtotals"] == (first, second)[index]["group_subtotals"]
    assert messages == before


def test_fold_oversized_single_subtotal_is_omitted_whole_and_fields_disclose_coverage():
    rows = [{"id": str(i), "fields": {"owner": "A", **{
        f"detail{n}": f"value-{i}-" + "m"*120 for n in range(3)}}} for i in range(35)]
    result = analyze(rows, group_by=["owner", "detail0", "detail1", "detail2"],
                     fields=["detail0", "detail1", "detail2"], max_chars=120000)
    assert len(result["group_subtotals"]) == 1
    folded = fold_statistics(result, "receipt")
    assert folded["group_subtotals"] == []
    scope = folded["_statistics_scope"]
    assert not scope["group_subtotals_complete"] and scope["returned_subtotal_group_count"] == 0
    assert scope["subtotal_group_count"] == 1
    assert scope["field_statistics_count"] == len(result["field_statistics"])
    assert not scope["field_statistics_complete"]
    assert scope["returned_field_statistics_count"] == len(folded["field_statistics"]) < len(result["field_statistics"])
    assert all(value in result["field_statistics"].values() for value in folded["field_statistics"].values())
