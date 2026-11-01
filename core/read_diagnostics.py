"""Private, context-local read observations. No I/O, business bodies or tokens."""
from contextvars import ContextVar
import hashlib
import json
import re
import time


READ_DIAGNOSTIC_TOOLS = {"list_records", "analyze_records", "analyze_cached_records"}
AUDIT_DIAGNOSTIC_KEY = "_opennexus_read_diagnostics"
_CURRENT = ContextVar("read_observation", default=None)
_EVENTS = {"business_http_attempt": "business_http_attempts", "business_http_failure": "business_http_failures",
           "analysis_cache_hit": "analysis_cache_hits", "analysis_cache_miss": "analysis_cache_misses",
           "local_snapshot_analysis": "local_snapshot_analyses"}
_COUNTERS = (*_EVENTS.values(), "elapsed_ms")
_HASHES = ("file_fingerprint", "sheet_fingerprint", "view_fingerprint", "filter_fingerprint",
           "fields_fingerprint", "start_cursor_fingerprint", "next_cursor_fingerprint",
           "record_set_fingerprint", "record_order_fingerprint", "local_filter_fingerprint",
           "grouping_fields_fingerprint")
_COUNTS = ("unique_record_ids", "source_records", "matched_records", "raw_returned_records",
           "analysis_returned_rows", "pages_fetched", "grouping_field_count", "returned_group_count",
           "returned_subtotal_count")
_FLAGS = ("source_scope_known", "source_complete", "has_more", "has_group_subtotals")


def begin_read_observation():
    state = {key: 0 for key in _EVENTS.values()}
    state.update(active=True, started=time.perf_counter(), identity={})
    return _CURRENT.set(state), state


def current_read_observation():
    state = _CURRENT.get()
    return state if state and state["active"] else None


def finish_read_observation(handle):
    token, state = handle
    state["active"] = False
    try:
        return {**{key: state[key] for key in _EVENTS.values()},
                "elapsed_ms": max(0, round((time.perf_counter() - state["started"]) * 1000)),
                "identity": dict(state["identity"])}
    finally:
        _CURRENT.reset(token)


def note_read_event(event):
    state = current_read_observation()
    if state is not None and event in _EVENTS:
        state[_EVENTS[event]] += 1


async def observed_business_request(call, *args, **kwargs):
    """Count SDK-level attempts only; preserve response, error and retry policy."""
    note_read_event("business_http_attempt")
    try:
        response = await call(*args, **kwargs)
    except BaseException:
        note_read_event("business_http_failure")
        raise
    if isinstance(getattr(response, "status_code", None), int) and response.status_code >= 400:
        note_read_event("business_http_failure")
    return response


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _record_identity(records):
    if not isinstance(records, list) or any(not isinstance(r, dict) or not r.get("id") for r in records):
        return {}
    identities = [str(r["id"]) for r in records]
    return {"unique_record_ids": len(set(identities)),
            "record_set_fingerprint": _fingerprint(sorted(set(identities))),
            "record_order_fingerprint": _fingerprint(identities)}


def note_source_records(records):
    state = current_read_observation()
    if state is not None:
        try:
            state["identity"] = _record_identity(records)
        except Exception:
            state["identity"] = {}  # Diagnostics must not replace a business result.


def public_read_diagnostics(value):
    """Only allow counters, flags and opaque hashes out of stored audit JSON."""
    if not isinstance(value, dict) or value.get("version") != 1 or value.get("transport_scope") != "wps_get_post_only":
        return {}
    def count(v):
        return v if type(v) is int and 0 <= v <= 10**12 else None
    counters = value.get("counters") if isinstance(value.get("counters"), dict) else {}
    proof = value.get("proof") if isinstance(value.get("proof"), dict) else {}
    return {"version": 1, "transport_scope": "wps_get_post_only",
            "counters": {key: count(counters.get(key)) for key in _COUNTERS},
            "proof": {**{key: (proof[key] if isinstance(proof.get(key), str)
                                 and re.fullmatch(r"[0-9a-f]{64}", proof[key]) else None) for key in _HASHES},
                      **{key: count(proof.get(key)) for key in _COUNTS},
                      **{key: proof[key] if type(proof.get(key)) is bool else None for key in _FLAGS}}}


def build_read_diagnostics(arguments, result, observation):
    """No raw arguments or response content survives this projection."""
    proof = {}
    try:
        result = result if isinstance(result, dict) else {}
        scope = result.get("_record_scope")
        known = isinstance(scope, dict) and bool(scope.get("file_id")) and scope.get("sheet_id") is not None
        proof["source_scope_known"] = known
        if known:
            for source, target in (("file_id", "file"), ("sheet_id", "sheet"), ("view_id", "view"),
                                   ("filter", "filter"), ("fields", "fields"), ("start_token", "start_cursor")):
                if source in scope:
                    proof[target + "_fingerprint"] = _fingerprint(scope[source])
        if "next_page_token" in result or "page_token" in result:
            proof["next_cursor_fingerprint"] = _fingerprint(result.get("next_page_token", result.get("page_token")))
        proof.update(observation.get("identity") or {})
        if "records" in result:
            proof.update(_record_identity(result["records"]))
            if isinstance(result["records"], list):
                proof["raw_returned_records"] = len(result["records"])
        for key in ("source_records", "matched_records", "pages_fetched"):
            proof[key] = result.get(key)
        proof["analysis_returned_rows"] = result.get("returned_rows")
        proof["source_complete"] = result.get("source_is_complete", result.get("is_complete"))
        proof["has_more"] = result.get("has_more")
        analysis = result.get("_analysis_scope") or {}
        if isinstance(analysis, dict) and "local_filters" in analysis:
            proof["local_filter_fingerprint"] = _fingerprint([analysis["local_filters"], analysis.get("local_filter_mode")])
        groups = arguments.get("group_by")
        if isinstance(groups, list):
            proof["grouping_fields_fingerprint"] = _fingerprint(groups)
            proof["grouping_field_count"] = len(groups)
        stats = result.get("_statistics_scope") or {}
        proof["has_group_subtotals"] = bool(result.get("group_subtotals"))
        if isinstance(stats, dict):
            proof["returned_group_count"] = stats.get("returned_group_count")
            proof["returned_subtotal_count"] = stats.get("returned_subtotal_group_count")
    except Exception:
        pass
    return public_read_diagnostics({"version": 1, "transport_scope": "wps_get_post_only",
                                    "counters": observation, "proof": proof})
