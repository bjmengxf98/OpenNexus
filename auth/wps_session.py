"""User-scoped WPS credential lifecycle; no business-write retries or model calls."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import inspect
import json
import re
import secrets
import time

import httpx

from auth import db, wps_oauth

_REQUEST_CONTEXT = ContextVar("wps_request_context", default=None)
_MESSAGES = {
    "disconnected": "尚未连接 WPS，请点击右上角连接。",
    "connected": "WPS 已连接。",
    "refreshing": "正在自动续期 WPS，暂不需要重新授权。",
    "reauth_required": "WPS 刷新凭证已失效或不可用，需要重新授权。",
    "temporary_error": "WPS 连接或自动续期暂时异常，稍后自动重试；尚不能认定授权失效。",
    "configuration_error": "WPS 应用授权配置异常，请联系管理员检查；重新登录未必能解决。",
}


class WpsConnectionError(RuntimeError):
    def __init__(self, status: str):
        self.status = status
        super().__init__(_MESSAGES[status])


def _fingerprint(row: dict) -> str:
    return hashlib.sha256(json.dumps([
        row.get("access_token") or "", row.get("refresh_token") or "",
    ]).encode()).hexdigest()


def _saved_state(user_id: int, row: dict) -> dict:
    try:
        state = json.loads(db.get_system_config(f"wps_connection_state:{int(user_id)}", "{}"))
    except (TypeError, ValueError):
        return {}
    return state if isinstance(state, dict) and state.get("credential_fingerprint") == _fingerprint(row) else {}


def _set_state(user_id: int, row: dict, status: str, *, retry_after: float = 0) -> None:
    db.save_wps_connection_state(user_id, row, {
        "credential_fingerprint": _fingerprint(row), "status": status,
        "retry_after": retry_after,
    })


def connection_snapshot(user_id: int) -> dict:
    row = db.get_wps_token(user_id) or {}
    state = _saved_state(user_id, row)
    if not row.get("access_token") and not row.get("refresh_token"):
        status = "disconnected"
    elif state.get("status") in {"reauth_required", "configuration_error", "temporary_error"}:
        status = state["status"]
    elif wps_oauth.is_token_expired(row.get("expires_at")) or not row.get("access_token"):
        status = "refreshing" if row.get("refresh_token") else "reauth_required"
    else:
        status = "connected"
    return {"connected": status == "connected", "status": status,
            "message": _MESSAGES[status], "username": row.get("wps_username") or "已连接",
            "connect_url": "/api/app-new/wps/connect", "checked_at": int(time.time() * 1000)}


def _refresh_failure(exc: Exception | dict) -> str:
    payload = exc if isinstance(exc, dict) else {}
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            payload = exc.response.json()
        except ValueError:
            payload = {}
        if exc.response.status_code >= 500 or exc.response.status_code == 429:
            return "temporary_error"
    if not isinstance(payload, dict):
        return "temporary_error"
    code = str(payload.get("error") or payload.get("code") or "").lower()
    message = str(payload.get("msg") or payload.get("error_description") or "").lower()
    if code in {"invalid_client", "unauthorized_client", "invalid_scope"}:
        return "configuration_error"
    if code in {"invalid_grant", "invalid_refresh_token"} or re.search(
        r"refresh[_ -]?token.{0,50}(?:invalid|expired|revoked)|"
        r"(?:invalid|expired|revoked).{0,50}refresh[_ -]?token|刷新.{0,12}(?:失效|过期|无效)",
        message,
    ):
        return "reauth_required"
    return "temporary_error"


async def ensure_access_token(user_id: int, *, force: bool = False,
                               rejected_token: str | None = None) -> str:
    """Refresh once under a cross-worker lease. Never persist malformed replies."""
    deadline = time.monotonic() + 15
    owner = secrets.token_hex(12)
    initial_pair = None
    while True:
        if time.monotonic() >= deadline:
            raise WpsConnectionError("refreshing")
        row = db.get_wps_token(user_id) or {}
        pair = _fingerprint(row)
        if initial_pair is None:
            initial_pair = pair
        state = _saved_state(user_id, row)
        if state.get("status") == "reauth_required":
            raise WpsConnectionError(state["status"])
        recoverable_failure = state.get("status") in {"temporary_error", "configuration_error"}
        if recoverable_failure and state.get("retry_after", 0) > time.time():
            if (not force and row.get("access_token")
                    and not wps_oauth.is_token_expired(row.get("expires_at"))):
                return row["access_token"]
            raise WpsConnectionError(state["status"])
        valid = row.get("access_token") and not wps_oauth.is_token_expiring_soon(row.get("expires_at"))
        # Another worker already rotated the rejected/initial credential.
        if valid and ((rejected_token is not None and row["access_token"] != rejected_token)
                      or pair != initial_pair or (not force and not recoverable_failure)):
            return row["access_token"]
        if not row.get("refresh_token"):
            if row.get("access_token") and not force and not wps_oauth.is_token_expired(row.get("expires_at")):
                return row["access_token"]
            status = "reauth_required" if row.get("access_token") else "disconnected"
            if row:
                _set_state(user_id, row, status)
            raise WpsConnectionError(status)
        if db.claim_wps_refresh_lease(user_id, owner):
            try:
                # Recheck after acquiring the lease, including concurrent disconnect.
                latest = db.get_wps_token(user_id) or {}
                if _fingerprint(latest) != pair:
                    continue
                try:
                    result = await wps_oauth.refresh_access_token(row["refresh_token"])
                    if (not isinstance(result, dict) or result.get("error")
                            or result.get("code", 0) not in (0, "0")
                            or not isinstance(result.get("access_token"), str)
                            or not result["access_token"].strip()
                            or int(result.get("expires_in", 0)) <= 60
                            or not isinstance(result.get("refresh_token", row["refresh_token"]), str)
                            or not result.get("refresh_token", row["refresh_token"])):
                        failure = _refresh_failure(result if isinstance(result, dict) else {})
                        raise WpsConnectionError(failure)
                except Exception as exc:
                    failure = exc.status if isinstance(exc, WpsConnectionError) else _refresh_failure(exc)
                    if _fingerprint(db.get_wps_token(user_id) or {}) != pair:
                        continue
                    _set_state(user_id, row, failure, retry_after=time.time() + 30
                               if failure in {"temporary_error", "configuration_error"} else 0)
                    if (failure in {"temporary_error", "configuration_error"} and not force
                            and row.get("access_token")
                            and not wps_oauth.is_token_expired(row.get("expires_at"))):
                        return row["access_token"]
                    raise WpsConnectionError(failure) from None
                saved = db.save_refreshed_wps_token(
                    user_id, row, result["access_token"], result.get("refresh_token", row["refresh_token"]),
                    wps_oauth.calc_expires_at(result["expires_in"]),
                )
                if not saved:
                    continue
                latest = db.get_wps_token(user_id) or {}
                _set_state(user_id, latest, "connected")
                return result["access_token"]
            finally:
                db.release_wps_refresh_lease(user_id, owner)
        if time.monotonic() >= deadline:
            raise WpsConnectionError("refreshing")
        await asyncio.sleep(0.1)


async def connection_status(user_id: int, *, refresh: bool = True) -> dict:
    row = db.get_wps_token(user_id) or {}
    if refresh and (row.get("access_token") or row.get("refresh_token")):
        try:
            await ensure_access_token(user_id)
        except WpsConnectionError as exc:
            snapshot = connection_snapshot(user_id)
            if exc.status == "refreshing":
                snapshot.update(connected=False, status=exc.status, message=str(exc))
            return snapshot
    return connection_snapshot(user_id)


@contextmanager
def request_context(user_id: int, token: str = "", on_status=None):
    marker = _REQUEST_CONTEXT.set({"user_id": int(user_id), "tokens": {token} if token else set(),
                                   "on_status": on_status})
    try:
        yield
    finally:
        _REQUEST_CONTEXT.reset(marker)


async def _publish(context: dict) -> None:
    if context.get("on_status"):
        snapshot = connection_snapshot(context["user_id"])
        comparison = {key: value for key, value in snapshot.items() if key != "checked_at"}
        if comparison != context.get("last_snapshot"):
            await context["on_status"](snapshot)
            context["last_snapshot"] = comparison


async def request_token(token: str) -> str:
    context = _REQUEST_CONTEXT.get()
    if not context:
        return token
    row = db.get_wps_token(context["user_id"]) or {}
    if token not in context["tokens"] and token != row.get("access_token"):
        return token  # App tokens / other credentials are never replaced by a user token.
    try:
        fresh = await ensure_access_token(context["user_id"])
        context["tokens"].update((token, fresh))
        return fresh
    finally:
        await _publish(context)


async def recover_rejected_token(token: str) -> str | None:
    context = _REQUEST_CONTEXT.get()
    if not context or token not in context["tokens"]:
        return None
    try:
        fresh = await ensure_access_token(context["user_id"], force=True, rejected_token=token)
        context["tokens"].add(fresh)
        return fresh
    finally:
        await _publish(context)


async def reject_refreshed_token(token: str) -> None:
    context = _REQUEST_CONTEXT.get()
    if context and token in context["tokens"]:
        row = db.get_wps_token(context["user_id"]) or {}
        if row.get("access_token") == token:
            _set_state(context["user_id"], row, "reauth_required")
            await _publish(context)


def authorized_chat(function):
    signature = inspect.signature(function)

    @wraps(function)
    async def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs).arguments
        uid = int(bound.get("uid") or 0)
        if not uid:
            return await function(*args, **kwargs)

        async def status(snapshot):
            if bound.get("on_agent_event"):
                await bound["on_agent_event"]("wps_status", snapshot)

        with request_context(uid, bound.get("access_token") or "", status):
            return await function(*args, **kwargs)
    return wrapped


def authorized_user_call(function):
    signature = inspect.signature(function)

    @wraps(function)
    async def wrapped(*args, **kwargs):
        uid = int(signature.bind(*args, **kwargs).arguments["user_id"])
        if _REQUEST_CONTEXT.get() and _REQUEST_CONTEXT.get()["user_id"] == uid:
            return await function(*args, **kwargs)
        with request_context(uid):
            return await function(*args, **kwargs)
    return wrapped
