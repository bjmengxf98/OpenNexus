"""WPS lifecycle contracts; temporary databases and simulated upstream only."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import db, wps_oauth
from auth import wps_session as session
from agent import wps_client
from agent import assistant as assistant_module
from api import app_new_routes


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "wps-session.db")
    db.init_db()
    # An accidental real refresh must fail, never reach the network.
    monkeypatch.setattr(wps_oauth, "refresh_access_token", AsyncMock(side_effect=AssertionError("unmocked refresh")))


def token(uid=1, *, expired=True, access="old", refresh="refresh-old"):
    expires = datetime.now(timezone.utc) + timedelta(hours=-1 if expired else 2)
    db.save_wps_token(uid, access, refresh, expires.isoformat(), f"wps-{uid}", f"name-{uid}")


def refreshed():
    return {"access_token": "fresh", "refresh_token": "refresh-fresh", "expires_in": 7200}


def test_normal_expiration_refreshes_and_preserves_identity(monkeypatch):
    token()
    refresh = AsyncMock(return_value=refreshed())
    monkeypatch.setattr(wps_oauth, "refresh_access_token", refresh)
    assert asyncio.run(session.ensure_access_token(1)) == "fresh"
    assert db.get_wps_token(1)["refresh_token"] == "refresh-fresh"
    assert db.get_wps_token(1)["wps_user_id"] == "wps-1"
    assert session.connection_snapshot(1)["connected"] is True
    refresh.assert_awaited_once_with("refresh-old")
    assert asyncio.run(session.ensure_access_token(1)) == "fresh"
    assert refresh.await_count == 1


def test_near_expiry_is_renewed_without_waiting_until_it_breaks(monkeypatch):
    token()
    row = db.get_wps_token(1)
    db.save_wps_token(1, row["access_token"], row["refresh_token"],
                      (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat())
    refresh = AsyncMock(return_value=refreshed())
    monkeypatch.setattr(wps_oauth, "refresh_access_token", refresh)
    assert asyncio.run(session.ensure_access_token(1)) == "fresh"
    assert refresh.await_count == 1


def test_refresh_only_credentials_can_recover(monkeypatch):
    token(access="")
    monkeypatch.setattr(wps_oauth, "refresh_access_token", AsyncMock(return_value=refreshed()))
    assert asyncio.run(session.ensure_access_token(1)) == "fresh"


def test_valid_credentials_without_refresh_token_are_not_prematurely_disabled():
    token(expired=False, refresh="")
    assert asyncio.run(session.ensure_access_token(1)) == "old"
    assert session.connection_snapshot(1)["status"] == "connected"


def test_simultaneous_refreshes_rotate_once(monkeypatch):
    token()

    async def refresh(_refresh):
        await asyncio.sleep(0.02)
        return refreshed()
    renew = AsyncMock(side_effect=refresh)
    monkeypatch.setattr(wps_oauth, "refresh_access_token", renew)

    async def run():
        return await asyncio.gather(*(session.ensure_access_token(1) for _ in range(6)))
    assert asyncio.run(run()) == ["fresh"] * 6
    assert renew.await_count == 1
    assert db.get_system_config("wps_refresh_lease:1") is None


def test_sqlite_lease_is_owner_checked_and_can_expire(monkeypatch):
    assert db.claim_wps_refresh_lease(1, "worker-a")
    assert not db.claim_wps_refresh_lease(1, "worker-b")
    assert db.claim_wps_refresh_lease(2, "worker-b")
    db.release_wps_refresh_lease(1, "not-owner")
    assert not db.claim_wps_refresh_lease(1, "worker-b")
    db.set_system_config("wps_refresh_lease:1", json.dumps({"owner": "worker-a", "until": 0}))
    assert db.claim_wps_refresh_lease(1, "worker-b")


@pytest.mark.parametrize("payload,expected", [
    ({"error": "invalid_grant"}, "reauth_required"),
    ({"code": 40100009, "msg": "invalid_grant"}, "reauth_required"),
    ({"code": 40100009, "msg": "  INVALID_GRANT  "}, "reauth_required"),
    ({"code": "invalid_grant"}, "reauth_required"),
    ({"code": 1, "msg": "invalid_refresh_token"}, "reauth_required"),
    ({"code": 1, "msg": "request failed", "error_description": "invalid_grant"}, "reauth_required"),
    ({"code": 1, "msg": "refresh_token is expired"}, "reauth_required"),
    ({"error": "invalid_client"}, "configuration_error"),
    ({"code": 1, "msg": "invalid_client"}, "configuration_error"),
    ({"code": 1, "msg": "unauthorized_client"}, "configuration_error"),
    ({"code": 1, "error_description": "invalid_scope"}, "configuration_error"),
    ({"code": 40100009}, "temporary_error"),
    ({"code": 1, "msg": "diagnostic mentions invalid_grant"}, "temporary_error"),
    ({"code": 1, "msg": "service temporarily unavailable"}, "temporary_error"),
    ({"access_token": "fresh", "expires_in": "invalid"}, "temporary_error"),
    ({"access_token": "fresh", "expires_in": 7200}, "temporary_error"),
    ({"access_token": "fresh", "refresh_token": "refresh-old", "expires_in": 7200}, "temporary_error"),
    ({"access_token": "fresh", "refresh_token": "   ", "expires_in": 7200}, "temporary_error"),
    ({"expires_in": 7200}, "temporary_error"),
    ({"access_token": "", "expires_in": 7200}, "temporary_error"),
    ({"access_token": "fresh", "refresh_token": "", "expires_in": 7200}, "temporary_error"),
])
def test_failed_or_malformed_refresh_is_classified_without_overwriting_tokens(monkeypatch, payload, expected):
    token()
    original = db.get_wps_token(1)
    renew = AsyncMock(return_value=payload)
    monkeypatch.setattr(wps_oauth, "refresh_access_token", renew)
    with pytest.raises(session.WpsConnectionError) as error:
        asyncio.run(session.ensure_access_token(1))
    assert error.value.status == expected
    assert db.get_wps_token(1) == original
    assert session.connection_snapshot(1)["status"] == expected
    assert session.connection_snapshot(1)["connected"] is False
    # No repeated refresh storms while a terminal error / temporary cooldown applies.
    with pytest.raises(session.WpsConnectionError):
        asyncio.run(session.ensure_access_token(1))
    assert renew.await_count == 1


def test_temporary_refresh_failure_can_recover_after_cooldown(monkeypatch):
    token()
    renew = AsyncMock(side_effect=httpx.ConnectError("do not expose credential-like details"))
    monkeypatch.setattr(wps_oauth, "refresh_access_token", renew)
    with pytest.raises(session.WpsConnectionError, match="暂时异常"):
        asyncio.run(session.ensure_access_token(1))
    row = db.get_wps_token(1)
    session._set_state(1, row, "temporary_error", retry_after=0)
    monkeypatch.setattr(wps_oauth, "refresh_access_token", AsyncMock(return_value=refreshed()))
    assert asyncio.run(session.ensure_access_token(1)) == "fresh"
    assert session.connection_snapshot(1)["status"] == "connected"


def test_preemptive_network_failure_does_not_disable_still_valid_access(monkeypatch):
    token()
    db.save_wps_token(1, "old", "refresh-old",
                      (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat())
    renew = AsyncMock(side_effect=httpx.ConnectError("temporary failure"))
    monkeypatch.setattr(wps_oauth, "refresh_access_token", renew)
    assert asyncio.run(session.ensure_access_token(1)) == "old"
    assert session.connection_snapshot(1)["status"] == "temporary_error"
    assert asyncio.run(session.ensure_access_token(1)) == "old"
    assert renew.await_count == 1
    # A token explicitly rejected by WPS is never reused merely because its clock is valid.
    with pytest.raises(session.WpsConnectionError):
        asyncio.run(session.ensure_access_token(1, force=True, rejected_token="old"))


@pytest.mark.parametrize("status,payload,expected", [
    (400, {"error": "invalid_grant"}, "reauth_required"),
    (400, {"code": 40100009, "msg": "invalid_grant"}, "reauth_required"),
    (401, {"error": "invalid_client"}, "configuration_error"),
    (401, {"code": 1, "msg": "invalid_client"}, "configuration_error"),
    (500, {"error": "invalid_grant"}, "temporary_error"),
    (500, {"code": 40100009, "msg": "invalid_grant"}, "temporary_error"),
    (429, {"error": "invalid_grant"}, "temporary_error"),
    (429, {"code": 40100009, "msg": "invalid_grant"}, "temporary_error"),
])
def test_http_refresh_errors_do_not_confuse_server_failure_with_user_revocation(monkeypatch, status, payload, expected):
    token()
    response = httpx.Response(status, json=payload, request=httpx.Request("POST", wps_oauth.WPS_TOKEN_URL))
    failure = httpx.HTTPStatusError("sensitive upstream response must not be shown", request=response.request, response=response)
    monkeypatch.setattr(wps_oauth, "refresh_access_token", AsyncMock(side_effect=failure))
    with pytest.raises(session.WpsConnectionError) as error:
        asyncio.run(session.ensure_access_token(1))
    assert error.value.status == expected
    assert "sensitive" not in str(error.value)
    assert db.get_wps_token(1)["access_token"] == "old"


@pytest.mark.parametrize("http_status", [200, 400])
def test_msg_invalid_grant_stops_refresh_until_new_authorization(monkeypatch, http_status):
    token()
    token(2, expired=False, access="other-user", refresh="other-refresh")
    original = db.get_wps_token(1)
    # Previously misclassified credentials must leave the yellow retry state.
    session._set_state(1, original, "temporary_error", retry_after=0)
    payload = {"code": 40100009, "msg": "invalid_grant"}
    if http_status == 200:
        renew = AsyncMock(return_value=payload)
    else:
        response = httpx.Response(http_status, json=payload,
                                  request=httpx.Request("POST", wps_oauth.WPS_TOKEN_URL))
        renew = AsyncMock(side_effect=httpx.HTTPStatusError(
            "mock refresh rejection", request=response.request, response=response))
    monkeypatch.setattr(wps_oauth, "refresh_access_token", renew)

    status = asyncio.run(session.connection_status(1))
    assert status["status"] == "reauth_required" and status["connected"] is False
    assert "重新授权" in status["message"]
    assert db.get_wps_token(1) == original
    assert session.connection_snapshot(2)["status"] == "connected"

    later = session.time.time() + 3600
    monkeypatch.setattr(session.time, "time", lambda: later)
    # Polling and forced recovery also stop after the former cooldown has elapsed.
    assert asyncio.run(session.connection_status(1))["status"] == "reauth_required"
    with pytest.raises(session.WpsConnectionError):
        asyncio.run(session.ensure_access_token(1, force=True))
    renew.assert_awaited_once_with("refresh-old")

    token(expired=False, access="new-login", refresh="new-login-refresh")
    assert asyncio.run(session.ensure_access_token(1)) == "new-login"
    assert session.connection_snapshot(1)["status"] == "connected"
    assert renew.await_count == 1


def test_configuration_failure_is_rechecked_after_admin_fix_and_cooldown(monkeypatch):
    token()
    session._set_state(1, db.get_wps_token(1), "configuration_error", retry_after=0)
    monkeypatch.setattr(wps_oauth, "refresh_access_token", AsyncMock(return_value=refreshed()))
    assert asyncio.run(session.ensure_access_token(1)) == "fresh"


@pytest.mark.parametrize("reauthorized", [False, True])
def test_inflight_refresh_cannot_resurrect_disconnect_or_overwrite_new_authorization(monkeypatch, reauthorized):
    token()

    async def refresh(_refresh):
        if reauthorized:
            token(expired=False, access="new-login", refresh="new-login-refresh")
        else:
            db.save_wps_token(1, "", "", "2000-01-01")
        return refreshed()
    monkeypatch.setattr(wps_oauth, "refresh_access_token", refresh)
    if reauthorized:
        assert asyncio.run(session.ensure_access_token(1)) == "new-login"
    else:
        with pytest.raises(session.WpsConnectionError, match="尚未连接"):
            asyncio.run(session.ensure_access_token(1))
        assert db.get_wps_token(1)["access_token"] == ""
    assert db.get_wps_token(1)["access_token"] != "fresh"


def test_reauthorization_ignores_old_rejected_state_and_keeps_users_isolated(monkeypatch):
    token(1)
    token(2, expired=False, access="other-user")
    session._set_state(1, db.get_wps_token(1), "reauth_required")
    token(1, expired=False, access="new-login", refresh="new-login-refresh")
    assert asyncio.run(session.ensure_access_token(1)) == "new-login"
    assert asyncio.run(session.ensure_access_token(2)) == "other-user"
    assert session.connection_snapshot(2)["status"] == "connected"


def upstream(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(wps_client.httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs,
    ))


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_read_401_forces_refresh_even_before_local_expiry_and_retries_once(monkeypatch, method):
    token(expired=False)
    renew = AsyncMock(return_value=refreshed())
    monkeypatch.setattr(wps_oauth, "refresh_access_token", renew)
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(401 if len(seen) == 1 else 200, json={"code": 0, "data": {"records": []}})
    upstream(monkeypatch, handler)
    status = AsyncMock()

    async def run():
        with session.request_context(1, "old", status):
            if method == "GET":
                return await wps_client._get("old", "/v7/coop/dbsheet/f/schema", signed=True)
            return await wps_client._post("old", "/v7/coop/dbsheet/f/sheets/1/records", {})
    assert asyncio.run(run())["code"] == 0
    assert seen == ["Bearer old", "Bearer fresh"]
    renew.assert_awaited_once_with("refresh-old")
    assert status.await_args.args[0]["connected"] is True
    assert session._REQUEST_CONTEXT.get() is None


@pytest.mark.parametrize("suffix", ["records/create", "records/update", "records/batch_delete", "unknown_operation"])
def test_writes_are_not_replayed_after_auth_recovery(monkeypatch, suffix):
    token(expired=False)
    renew = AsyncMock(return_value=refreshed())
    monkeypatch.setattr(wps_oauth, "refresh_access_token", renew)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={"code": "invalid_token"})
    upstream(monkeypatch, handler)

    async def run():
        with session.request_context(1, "old"):
            await wps_client._post("old", "/v7/coop/dbsheet/f/sheets/1/" + suffix, {})
    with pytest.raises(RuntimeError, match="未自动重放"):
        asyncio.run(run())
    assert len(calls) == 1 and renew.await_count == 1
    assert session.connection_snapshot(1)["status"] == "connected"


def test_second_business_401_does_not_prove_global_revocation(monkeypatch):
    token(expired=False)
    monkeypatch.setattr(wps_oauth, "refresh_access_token", AsyncMock(return_value=refreshed()))
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(401)
    upstream(monkeypatch, handler)

    async def run():
        with session.request_context(1, "old"):
            await wps_client._get("old", "/v7/coop/dbsheet/f/schema")
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())
    assert len(requests) == 3  # Two business reads and one bounded identity probe.
    assert session.connection_snapshot(1)["status"] == "temporary_error"


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_business_401_keeps_verified_identity_connected(monkeypatch, method):
    token(expired=False)
    renew = AsyncMock(return_value=refreshed())
    monkeypatch.setattr(wps_oauth, "refresh_access_token", renew)
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v7/users/current_id":
            assert request.headers["authorization"] == "Bearer fresh"
            return httpx.Response(200, json={"code": 0, "data": {"user_id": "wps-1"}})
        return httpx.Response(401, json={"msg": "business endpoint authentication failed"})
    upstream(monkeypatch, handler)

    async def run():
        with session.request_context(1, "old"):
            if method == "GET":
                await wps_client._get("old", "/v7/coop/dbsheet/f/schema")
            else:
                await wps_client._post("old", "/v7/coop/dbsheet/f/sheets/1/records", {})
    with pytest.raises(httpx.HTTPStatusError) as error:
        asyncio.run(run())
    assert len(requests) == 3
    assert "已通过独立核验" in str(error.value)
    assert error.value.response.status_code == 401
    assert session.connection_snapshot(1)["status"] == "connected"
    assert asyncio.run(session.ensure_access_token(1)) == "fresh"
    renew.assert_awaited_once()


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("new_authorization", [False, True])
def test_post_save_disconnect_or_new_login_cannot_send_previous_credential(monkeypatch, new_authorization, method):
    token()
    monkeypatch.setattr(wps_oauth, "refresh_access_token", AsyncMock(return_value=refreshed()))
    original = db.save_refreshed_wps_token

    def interleave(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result
        if new_authorization:
            token(expired=False, access="new-login", refresh="new-login-refresh")
        else:
            db.save_wps_token(1, "", "", "2000-01-01")
        return result
    monkeypatch.setattr(db, "save_refreshed_wps_token", interleave)
    observed = []

    def handler(request):
        observed.append(request.headers["authorization"])
        return httpx.Response(200, json={"code": 0})
    upstream(monkeypatch, handler)

    async def run():
        with session.request_context(1, "old"):
            if method == "POST":
                return await wps_client._post("old", "/v7/coop/dbsheet/f/sheets/1/records/create", {})
            return await wps_client._get("old", "/v7/coop/dbsheet/f/schema")
    if new_authorization:
        assert asyncio.run(run())["code"] == 0
        assert observed == ["Bearer new-login"]
    else:
        with pytest.raises(session.WpsConnectionError):
            asyncio.run(run())
        assert not observed
        assert session.connection_snapshot(1)["status"] == "disconnected"


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("new_authorization", [False, True])
def test_status_callback_race_is_checked_before_http_dispatch(monkeypatch, new_authorization, method):
    token(expired=False)
    observed = []

    async def status(_snapshot):
        if new_authorization:
            token(expired=False, access="new-login", refresh="new-login-refresh")
        else:
            db.save_wps_token(1, "", "", "2000-01-01")

    def handler(request):
        observed.append(request.headers["authorization"])
        return httpx.Response(200, json={"code": 0})
    upstream(monkeypatch, handler)

    async def run():
        with session.request_context(1, "old", status):
            if method == "POST":
                return await wps_client._post("old", "/v7/coop/dbsheet/f/sheets/1/records/create", {})
            return await wps_client._get("old", "/v7/coop/dbsheet/f/schema")
    if new_authorization:
        asyncio.run(run())
        assert observed == ["Bearer new-login"]
    else:
        with pytest.raises(session.WpsConnectionError):
            asyncio.run(run())
        assert not observed


@pytest.mark.parametrize("valid_refresh", [False, True])
def test_legacy_red_state_gets_one_revalidation_and_confirmed_rejection_stops(monkeypatch, valid_refresh):
    token(expired=False)
    previous = db.get_wps_token(1)
    db.save_wps_connection_state(1, previous, {
        "credential_fingerprint": session._fingerprint(previous),
        "status": "reauth_required", "retry_after": 0,
    })
    async def refresh(_token):
        await asyncio.sleep(0.01)
        return refreshed() if valid_refresh else {"error": "invalid_grant"}
    renew = AsyncMock(side_effect=refresh)
    monkeypatch.setattr(wps_oauth, "refresh_access_token", renew)
    expected = "connected" if valid_refresh else "reauth_required"
    async def simultaneous_checks():
        return await asyncio.gather(*(session.connection_status(1) for _ in range(4)))
    assert {status["status"] for status in asyncio.run(simultaneous_checks())} == {expected}
    assert asyncio.run(session.connection_status(1))["status"] == expected
    renew.assert_awaited_once()


@pytest.mark.parametrize("probe_case", ["401", "403", "500", "429", "malformed", "other_user", "timeout"])
def test_failed_identity_probe_stays_uncertain_and_bounded(monkeypatch, probe_case):
    token(expired=False)
    renew = AsyncMock(return_value=refreshed())
    monkeypatch.setattr(wps_oauth, "refresh_access_token", renew)
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path != "/v7/users/current_id":
            return httpx.Response(401)
        if probe_case == "timeout":
            raise httpx.ReadTimeout("sensitive diagnostic must not leak")
        if probe_case == "malformed":
            return httpx.Response(200, text="not JSON")
        if probe_case == "other_user":
            return httpx.Response(200, json={"code": 0, "data": {"user_id": "not-this-user"}})
        return httpx.Response(int(probe_case))
    upstream(monkeypatch, handler)

    async def run():
        with session.request_context(1, "old"):
            await wps_client._get("old", "/v7/coop/dbsheet/f/schema")
    with pytest.raises(httpx.HTTPStatusError) as error:
        asyncio.run(run())
    assert len(calls) == 3
    assert "尚不能认定授权失效" in str(error.value)
    snapshot = session.connection_snapshot(1)
    assert snapshot["status"] == "temporary_error"
    assert "sensitive" not in json.dumps(snapshot)
    assert asyncio.run(session.ensure_access_token(1)) == "fresh"
    renew.assert_awaited_once()


@pytest.mark.parametrize("new_authorization", [False, True])
def test_identity_probe_cannot_overwrite_new_connection_state(monkeypatch, new_authorization):
    token(expired=False)
    monkeypatch.setattr(wps_oauth, "refresh_access_token", AsyncMock(return_value=refreshed()))

    def handler(request):
        if request.url.path == "/v7/users/current_id":
            if new_authorization:
                token(expired=False, access="new-login", refresh="new-login-refresh")
            else:
                db.save_wps_token(1, "", "", "2000-01-01")
            return httpx.Response(200, json={"code": 0, "data": {"user_id": "wps-1"}})
        return httpx.Response(401)
    upstream(monkeypatch, handler)

    async def run():
        with session.request_context(1, "old"):
            await wps_client._get("old", "/v7/coop/dbsheet/f/schema")
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())
    assert session.connection_snapshot(1)["status"] == ("connected" if new_authorization else "disconnected")
    assert db.get_wps_token(1)["access_token"] == ("new-login" if new_authorization else "")


@pytest.mark.parametrize("status_code,api_token", [(403, "old"), (401, "app-level-token")])
def test_file_permission_failures_and_app_tokens_do_not_rotate_user_credentials(monkeypatch, status_code, api_token):
    token(expired=False)
    upstream(monkeypatch, lambda _request: httpx.Response(status_code))

    async def run():
        with session.request_context(1, "old"):
            await wps_client._get(api_token, "/v7/coop/dbsheet/f/schema")
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())
    wps_oauth.refresh_access_token.assert_not_awaited()
    assert session.connection_snapshot(1)["status"] == "connected"


def test_concurrent_rotation_is_reused_and_context_does_not_leak(monkeypatch):
    token(expired=False)

    async def run():
        with session.request_context(1, "old"):
            token(expired=False, access="rotated", refresh="rotated-refresh")
            assert await session.request_token("old") == "rotated"
            assert await session.recover_rejected_token("old") == "rotated"
        assert await session.request_token("app-level-token") == "app-level-token"
    asyncio.run(run())
    wps_oauth.refresh_access_token.assert_not_awaited()


def api(monkeypatch, uid=1):
    monkeypatch.setattr(app_new_routes, "_current_user", lambda _request: (
        (uid, {"username": "tester", "display_name": "Tester", "role": "staff"}) if uid else (None, None)
    ))
    app = FastAPI()
    app.include_router(app_new_routes.app_new_router)
    return TestClient(app)


def test_status_endpoint_is_owner_scoped_no_store_and_never_exposes_tokens(monkeypatch):
    token(1, expired=False)
    token(2, expired=False, access="other-user")
    session._set_state(1, db.get_wps_token(1), "reauth_required")
    client = api(monkeypatch, 1)
    response = client.get("/api/app-new/wps/status?user_id=2")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    data = response.json()["wps"]
    assert data["status"] == "reauth_required" and data["connected"] is False
    assert "access_token" not in response.text and "refresh_token" not in response.text
    assert "credential_fingerprint" not in response.text and "other-user" not in response.text
    assert api(monkeypatch, 0).get("/api/app-new/wps/status").status_code == 401


def test_bootstrap_auto_refreshes_and_reauth_link_is_created_only_on_click(monkeypatch):
    token()
    renew = AsyncMock(return_value=refreshed())
    monkeypatch.setattr(wps_oauth, "refresh_access_token", renew)
    build = lambda uid: f"https://example.test/authorize?user={uid}"
    monkeypatch.setattr(app_new_routes, "build_auth_url", build)
    client = api(monkeypatch)
    response = client.get("/api/app-new/bootstrap")
    assert response.json()["wps"]["connected"] is True
    assert response.json()["wps"]["connect_url"] == "/api/app-new/wps/connect"
    response = client.get("/api/app-new/wps/connect", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://example.test/authorize?user=1"


@pytest.mark.parametrize("temporary_failure", [False, True])
def test_real_chat_guard_recovers_empty_token_or_reports_exact_failure(monkeypatch, temporary_failure):
    token()
    if temporary_failure:
        session._set_state(1, db.get_wps_token(1), "temporary_error", retry_after=10**12)
    else:
        monkeypatch.setattr(wps_oauth, "refresh_access_token", AsyncMock(return_value=refreshed()))
    read = AsyncMock(return_value={"sheets": []})
    monkeypatch.setitem(assistant_module.TOOL_MAP, "get_schema", read)
    responses = []
    for i in range(1 if temporary_failure else 2):
        call = SimpleNamespace(id=f"read-{i}", function=SimpleNamespace(
            name="get_schema", arguments=json.dumps({"file_id": "f"}),
        ))
        responses.append(SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=None, tool_calls=[call], reasoning_content=None,
        ))]))
    responses.append(SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="已说明真实状态。", tool_calls=None, reasoning_content=None,
    ))]))
    agent = assistant_module.Assistant.__new__(assistant_module.Assistant)
    agent.provider, agent.model = "mock", "mock"
    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(side_effect=responses),
    )))
    agent.supports_tools = True
    agent.smart_tool_routing = True
    agent._auto_learn = AsyncMock()
    asyncio.run(agent.chat([{"role": "user", "content": "看一下表结构"}], "", uid=1,
                           default_file={"file_id": "f", "file_name": "测试表"}))
    result = agent.last_tool_receipts[0]["result"]
    if temporary_failure:
        assert result["code"] == "wps_temporary_error"
        assert "暂时异常" in result["error"] and result["reauth_required"] is False
        read.assert_not_called()
    else:
        assert result["code"] == "wps_recovered"
        read.assert_awaited_once_with("fresh", {"file_id": "f"})


def test_chat_request_context_emits_live_auth_failure_without_tokens(monkeypatch):
    token(expired=False)
    monkeypatch.setattr(wps_oauth, "refresh_access_token", AsyncMock(return_value={"error": "invalid_grant"}))
    upstream(monkeypatch, lambda _request: httpx.Response(401))
    emit = AsyncMock()

    @session.authorized_chat
    async def chat(self, messages, access_token, uid=0, on_agent_event=None):
        return await wps_client._get(access_token, "/v7/coop/dbsheet/f/schema")

    with pytest.raises(session.WpsConnectionError):
        asyncio.run(chat(None, [], "old", uid=1, on_agent_event=emit))
    assert emit.await_args.args[0] == "wps_status"
    assert emit.await_args.args[1]["status"] == "reauth_required"
    assert "access_token" not in emit.await_args.args[1]
    assert session._REQUEST_CONTEXT.get() is None


@pytest.mark.parametrize("value", [None, "", "not-a-date", "2000-01-01T00:00:00", "2000-01-01T00:00:00+08:00"])
def test_bad_or_expired_dates_fail_closed_without_crashing(value):
    assert wps_oauth.is_token_expired(value)


def test_legacy_naive_and_utc_dates_remain_compatible():
    assert not wps_oauth.is_token_expired((datetime.now() + timedelta(hours=1)).isoformat())
    assert not wps_oauth.is_token_expired((datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
