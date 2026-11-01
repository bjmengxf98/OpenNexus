"""Opaque read evidence and observed transport/cache counters, offline only."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from auth import db
from core import record_evidence as evidence


def diagnostics():
    from core import read_diagnostics
    return read_diagnostics


def page(ids, *, start="", next_token="next", filter=None, view_id=None):
    return evidence.normalize_page({"records": [{"id": key, "fields": {"private-field": "private-value"}} for key in ids],
                                    "page_token": next_token}, scope=evidence.record_scope(
                                        file_id="private-file", sheet_id=7, fields=["private-field"],
                                        filter=filter, view_id=view_id, start_token=start))


def test_scope_and_identity_proofs_distinguish_cursor_order_and_membership_without_raw_values():
    d = diagnostics()
    one = page(["record-a", "record-b"], filter={"secret-filter": "secret-value"}, view_id="secret-view")
    before = deepcopy(one)
    first = d.build_read_diagnostics({}, one, {})
    reordered = d.build_read_diagnostics({}, page(["record-b", "record-a"],
        filter={"secret-filter": "secret-value"}, view_id="secret-view"), {})
    other = d.build_read_diagnostics({}, page(["record-a", "record-c"]), {})
    tail = d.build_read_diagnostics({}, page(["record-a", "record-b"], start="next"), {})
    assert first["proof"]["record_set_fingerprint"] == reordered["proof"]["record_set_fingerprint"]
    assert first["proof"]["record_order_fingerprint"] != reordered["proof"]["record_order_fingerprint"]
    assert first["proof"]["record_set_fingerprint"] != other["proof"]["record_set_fingerprint"]
    assert first["proof"]["start_cursor_fingerprint"] != tail["proof"]["start_cursor_fingerprint"]
    assert first["proof"]["view_fingerprint"] != other["proof"]["view_fingerprint"]
    assert first["proof"]["unique_record_ids"] == 2
    encoded = json.dumps(first)
    assert not any(value in encoded for value in ["private-file", "private-field", "private-value",
                                                 "record-a", "secret-value", "secret-view", "secret-filter", '"next"'])
    assert one == before


def test_missing_record_scope_and_identity_are_unknown_not_invented_empty():
    result = diagnostics().build_read_diagnostics({}, {"error": "failed"}, {})
    assert result["proof"]["source_scope_known"] is False
    assert result["proof"]["unique_record_ids"] is None
    assert result["proof"]["record_set_fingerprint"] is None


def test_missing_scope_component_is_not_fingerprinted_as_explicit_no_filter():
    d=diagnostics()
    missing=d.build_read_diagnostics({}, {"_record_scope":{"file_id":"f","sheet_id":7}}, {})["proof"]
    explicit=d.build_read_diagnostics({}, {"_record_scope":{"file_id":"f","sheet_id":7,"filter":None}}, {})["proof"]
    assert missing["source_scope_known"] is True
    assert missing["filter_fingerprint"] is None
    assert explicit["filter_fingerprint"] is not None


def test_observations_are_nested_isolated_and_ignore_late_inherited_tasks():
    d = diagnostics()
    async def run():
        outer = d.begin_read_observation()
        inner = d.begin_read_observation()
        d.note_read_event("analysis_cache_hit")
        assert d.finish_read_observation(inner)["analysis_cache_hits"] == 1
        gate = asyncio.Event()
        async def late():
            await gate.wait()
            d.note_read_event("analysis_cache_hit")
        job = asyncio.create_task(late())
        result = d.finish_read_observation(outer)
        gate.set();await job
        assert result["analysis_cache_hits"] == 0
        assert d.current_read_observation() is None
    asyncio.run(run())


def test_parallel_observations_and_cancelled_http_do_not_leak_context():
    d = diagnostics()
    async def run():
        async def one(count):
            handle = d.begin_read_observation()
            for _ in range(count):
                d.note_read_event("analysis_cache_hit")
                await asyncio.sleep(0)
            return d.finish_read_observation(handle)
        results = await asyncio.gather(one(1), one(3))
        assert [r["analysis_cache_hits"] for r in results] == [1,3]
        handle = d.begin_read_observation()
        async def cancelled():raise asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await d.observed_business_request(cancelled)
        assert d.finish_read_observation(handle)["business_http_failures"] == 1
        assert d.current_read_observation() is None
    asyncio.run(run())


@pytest.mark.parametrize("method,path,expected", [
    ("GET","/read",2), ("POST","/v7/coop/dbsheet/f/sheets/7/records",2),
    ("POST","/write",1),
])
def test_transport_counts_observed_attempts_without_changing_safe_retry_policy(monkeypatch, method, path, expected):
    from agent import wps_client as wps
    d = diagnostics()
    calls = []
    async def handler(request):
        calls.append(request)
        return httpx.Response(401 if len(calls)==1 else 200, json={"code":0,"data":{}})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(wps.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(wps, "request_token", AsyncMock(side_effect=lambda token: token))
    monkeypatch.setattr(wps, "validate_request_token", lambda token: token)
    monkeypatch.setattr(wps, "recover_rejected_token", AsyncMock(return_value="new-secret-token"))
    async def run():
        handle = d.begin_read_observation()
        try:
            if method=="GET":await wps._get("secret-token",path)
            elif expected==1:
                with pytest.raises(RuntimeError, match="未自动重放"):
                    await wps._post("secret-token",path,{"secret-content":"body"})
            else:await wps._post("secret-token",path,{})
        finally:result=d.finish_read_observation(handle)
        assert result["business_http_attempts"] == expected
        assert result["business_http_failures"] == 1
        assert "secret" not in json.dumps(result)
    asyncio.run(run())
    assert len(calls)==expected


def test_transport_exception_is_unchanged_and_not_copied_into_diagnostics():
    d = diagnostics();error = httpx.ConnectTimeout("private error body")
    async def request():raise error
    async def run():
        handle = d.begin_read_observation()
        with pytest.raises(httpx.ConnectTimeout) as caught:
            await d.observed_business_request(request)
        result=d.finish_read_observation(handle)
        assert caught.value is error and result["business_http_attempts"]==1
        assert result["business_http_failures"]==1 and "private" not in json.dumps(result)
    asyncio.run(run())


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(db,"DB_PATH",tmp_path/"diagnostics.db")
    db.init_db()


def test_source_cache_hit_is_distinct_from_remote_read_and_local_snapshot_analysis(monkeypatch):
    from agent import record_analyzer as analyzer
    d=diagnostics()
    monkeypatch.setattr(analyzer,"_CACHE",analyzer.OrderedDict())
    read=AsyncMock(return_value=page(["record-a"],next_token=""))
    monkeypatch.setattr(analyzer,"list_records",read)
    async def run():
        handle=d.begin_read_observation()
        result=await analyzer.analyze_records("secret-token",{"file_id":"f","sheet_id":7})
        first=d.finish_read_observation(handle)
        handle=d.begin_read_observation()
        again=await analyzer.analyze_records("secret-token",{"file_id":"f","sheet_id":7})
        second=d.finish_read_observation(handle)
        handle=d.begin_read_observation()
        analyzer.analyze_cached_records(page(["record-a"]),{})
        third=d.finish_read_observation(handle)
        assert first["analysis_cache_misses"]==1 and first["analysis_cache_hits"]==0
        assert second["analysis_cache_hits"]==1 and second["business_http_attempts"]==0
        assert third["local_snapshot_analyses"]==1
        assert result==again
        proof=d.build_read_diagnostics({},again,second)["proof"]
        assert proof["unique_record_ids"]==1
    asyncio.run(run())
    assert read.await_count==1


def test_audit_read_diagnostics_are_bounded_and_do_not_expose_caller_injected_text(isolated):
    d=diagnostics()
    safe=d.build_read_diagnostics({},page(["record-a"]),{})
    polluted=deepcopy(safe);polluted["private-body"]="secret"
    polluted["proof"]["file_fingerprint"]="private-file"
    public=d.public_read_diagnostics(polluted)
    assert "secret" not in json.dumps(public) and "private-file" not in json.dumps(public)
    db.add_mcp_audit_log(1,None,"list_records",json.dumps({"_opennexus_read_diagnostics":safe}),True)
    assert db.list_mcp_audit_log(1)[0]["read_diagnostics"]==safe
    assert db.list_mcp_audit_log(2)==[]


def test_chat_persists_opaque_scope_identity_and_local_computation_events(monkeypatch, isolated):
    from agent import assistant
    from test_current_goal_repair import make_assistant,response
    d=diagnostics()
    read=AsyncMock(return_value=page(["record-a","record-b"]))
    monkeypatch.setattr(assistant,"list_records",read)
    create=AsyncMock(side_effect=[response(name="list_records",args={"file_id":"f","sheet_id":7,"page_size":2}),
        response(name="analyze_cached_records",args={"receipt_index":0,"group_by":["private-field"]}),
        response("当前两条，仅局部范围。")])
    agent=make_assistant(create);events=[]
    async def on_event(kind,payload):events.append((kind,payload))
    asyncio.run(agent.chat([{"role":"user","content":"只汇总已读这两条"}],"secret-token",uid=1,
                           on_agent_event=on_event))
    proofs=[p for k,p in events if k=="evidence"]
    assert len(proofs)==2
    assert all(p["read_diagnostics"]["proof"]["unique_record_ids"]==2 for p in proofs)
    assert proofs[1]["read_diagnostics"]["counters"]["local_snapshot_analyses"]==1
    assert "secret-token" not in json.dumps(proofs)
    assert read.await_count==1 and d.current_read_observation() is None


def test_mcp_audit_observes_read_without_changing_tool_result_or_permissions(monkeypatch, isolated):
    from core import mcp_server as service
    from agent import assistant
    d=diagnostics();raw=page(["record-a"]);before=deepcopy(raw)
    identity={"user_id":1,"id":3,"scopes":["read"],"username":"u"}
    monkeypatch.setattr(service,"_identity",lambda:identity)
    monkeypatch.setattr(service,"_wps_access_token",AsyncMock(return_value="secret-token"))
    async def read(token,args):
        d.note_read_event("analysis_cache_hit")
        return raw
    monkeypatch.setitem(assistant.TOOL_MAP,"list_records",read)
    result=asyncio.run(service.execute_tool("list_records",{"file_id":"f","sheet_id":7}))
    assert result==before and raw==before
    audit=db.list_mcp_audit_log(1)[0]
    assert audit["read_diagnostics"]["counters"]["analysis_cache_hits"]==1
    assert audit["read_diagnostics"]["proof"]["unique_record_ids"]==1
    assert d.current_read_observation() is None


def test_cancelled_mcp_read_is_not_audited_as_success(monkeypatch, isolated):
    from core import mcp_server as service
    from agent import assistant
    d=diagnostics()
    monkeypatch.setattr(service,"_identity",lambda:{"user_id":1,"id":3,"scopes":["read"],"username":"u"})
    monkeypatch.setattr(service,"_wps_access_token",AsyncMock(return_value="secret-token"))
    async def cancelled():raise asyncio.CancelledError()
    async def read(token,args):
        return await d.observed_business_request(cancelled)
    monkeypatch.setitem(assistant.TOOL_MAP,"list_records",read)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.execute_tool("list_records",{"file_id":"f","sheet_id":7}))
    audit=db.list_mcp_audit_log(1)[0]
    assert audit["success"]==0 and audit["decision"]=="failed"
    assert audit["read_diagnostics"]["counters"]["business_http_failures"]==1
    assert d.current_read_observation() is None


def test_injected_audit_metadata_is_removed_and_truncated_old_json_has_no_fake_counters(isolated):
    from core import tool_governance as governance
    d=diagnostics()
    context=governance.new_tool_context({"user_id":1,"scopes":["read"]},"get_current_user",
        {d.AUDIT_DIAGNOSTIC_KEY:d.build_read_diagnostics({},page(["record-a"]),{})})
    asyncio.run(governance.run_post_tool_hooks(context,{"ok":True},""))
    assert "read_diagnostics" not in db.list_mcp_audit_log(1)[0]
    db.add_mcp_audit_log(1,None,"list_records",'{"_opennexus_read_diagnostics":',False)
    assert "read_diagnostics" not in db.list_mcp_audit_log(1)[0]


def test_diagnostic_hash_failure_never_rewrites_or_raises_business_result(monkeypatch):
    d=diagnostics();raw=page(["record-a"]);before=deepcopy(raw)
    def fail(value):raise RuntimeError("private serializer failure")
    monkeypatch.setattr(d,"_fingerprint",fail)
    result=d.build_read_diagnostics({},raw,{})
    assert raw==before and result["proof"]["record_set_fingerprint"] is None
    assert "private serializer failure" not in json.dumps(result)


def test_large_scope_proof_is_bounded_and_excludes_large_fields_and_body():
    d=diagnostics()
    raw=page([str(i) for i in range(3000)],filter={"large_filter":"secret-"*10000})
    result=d.build_read_diagnostics({"group_by":["field"]},raw,{})
    assert len(json.dumps(result))<4000
    assert result["proof"]["unique_record_ids"]==3000
    assert "secret-" not in json.dumps(result)


def test_authorization_failure_before_dispatch_is_zero_observed_http_attempts(monkeypatch):
    from agent import wps_client as wps
    d=diagnostics()
    monkeypatch.setattr(wps,"request_token",AsyncMock(side_effect=RuntimeError("not connected")))
    client=AsyncMock(side_effect=AssertionError("transport must not start"))
    monkeypatch.setattr(wps.httpx,"AsyncClient",client)
    async def run():
        handle=d.begin_read_observation()
        with pytest.raises(RuntimeError,match="not connected"):
            await wps._get("secret-token","/read")
        result=d.finish_read_observation(handle)
        assert result["business_http_attempts"]==0 and result["business_http_failures"]==0
    asyncio.run(run())
    client.assert_not_called()


def test_mcp_denied_read_is_logged_without_acquiring_wps_token(monkeypatch, isolated):
    from core import mcp_server as service
    from mcp.server.fastmcp.exceptions import ToolError
    monkeypatch.setattr(service,"_identity",lambda:{"user_id":1,"id":3,"scopes":["manage_memory"],"username":"u"})
    token=AsyncMock(side_effect=AssertionError("denied read must not request token"))
    monkeypatch.setattr(service,"_wps_access_token",token)
    with pytest.raises(ToolError):
        asyncio.run(service.execute_tool("list_records",{"file_id":"f","sheet_id":7}))
    audit=db.list_mcp_audit_log(1)[0]
    assert audit["decision"]=="deny" and audit["success"]==0
    assert audit["read_diagnostics"]["counters"]["business_http_attempts"]==0
    token.assert_not_awaited()
