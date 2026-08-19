"""
WPS OAuth2 授权流程
"""
import httpx
import logging
import os
import secrets
from urllib.parse import urlencode
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

WPS_AUTH_URL = "https://openapi.wps.cn/oauth2/auth"
WPS_TOKEN_URL = "https://openapi.wps.cn/oauth2/token"
WPS_USER_URL = "https://openapi.wps.cn/oauthapi/v3/user/info"

# 默认值（管理后台未配置时使用）
_ENV_APP_ID = os.environ.get("WPS_APP_ID", "").strip()
_ENV_APP_SECRET = os.environ.get("WPS_APP_SECRET", "").strip()

_LOCAL_REDIRECT_URI = "http://localhost:8000/oauth/callback"
# 保留该常量供旧代码导入；授权和换 token 必须调用下面的动态函数。
REDIRECT_URI = os.environ.get("WPS_REDIRECT_URI", "").strip() or _LOCAL_REDIRECT_URI


def get_redirect_uri() -> str:
    """Return the single canonical OAuth callback used by auth and token exchange.

    Production should set ``WPS_REDIRECT_URI`` explicitly. ``APP_BASE_URL`` is a
    safe fallback for deployments which only configure their public base URL.
    """
    explicit = os.environ.get("WPS_REDIRECT_URI", "").strip()
    if explicit:
        return explicit

    app_base_url = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
    if app_base_url:
        return f"{app_base_url}/oauth/callback"

    return _LOCAL_REDIRECT_URI


def get_app_id() -> str:
    from auth.db import get_system_config
    return get_system_config("wps_app_id") or _ENV_APP_ID


def get_app_secret() -> str:
    from auth.db import get_system_config
    return get_system_config("wps_app_secret") or _ENV_APP_SECRET


SCOPE = (
    "kso.dbsheet.readwrite "
    "kso.documents.readwrite "
    "kso.contact.read "
    "kso.file.readwrite "
    "kso.coop_files.readwrite "
    "kso.task.readwrite "
    "kso.calendar_events.readwrite "
    "kso.workflow_approval_instance.readwrite "
    "kso.workflow_approval_define.read "
    "kso.mail.readwrite "
    "kso.mailbox.read "
    "kso.user_current_id.read "
    "kso.user_base.read "
    "kso.sheets.readwrite"
)

# 临时存储 state -> user_id 映射（生产环境用Redis）
_state_store: dict[str, int] = {}


def build_auth_url(user_id: int) -> str:
    state = secrets.token_urlsafe(16)
    _state_store[state] = user_id
    redirect_uri = get_redirect_uri()
    logger.info("[WPS OAUTH] authorization redirect_uri=%s", redirect_uri)
    params = {
        "response_type": "code",
        "client_id": get_app_id(),
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "state": state,
    }
    return f"{WPS_AUTH_URL}?{urlencode(params)}"


def pop_state(state: str) -> int | None:
    return _state_store.pop(state, None)


async def exchange_code(code: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(WPS_TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": get_redirect_uri(),
            "client_id": get_app_id(),
            "client_secret": get_app_secret(),
        })
        resp.raise_for_status()
        return resp.json()


async def refresh_access_token(refresh_token: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(WPS_TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": get_app_id(),
            "client_secret": get_app_secret(),
        })
        resp.raise_for_status()
        return resp.json()


async def get_wps_user_info(access_token: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(WPS_USER_URL, headers={
            "Authorization": f"Bearer {access_token}"
        })
        resp.raise_for_status()
        return resp.json()


def calc_expires_at(expires_in: int) -> str:
    return (datetime.now() + timedelta(seconds=expires_in - 60)).isoformat()


def is_token_expired(expires_at: str) -> bool:
    return datetime.now() >= datetime.fromisoformat(expires_at)


def is_token_expiring_soon(expires_at: str, minutes: int = 5) -> bool:
    """检查 token 是否即将过期（默认5分钟内）"""
    try:
        exp_time = datetime.fromisoformat(expires_at)
        return datetime.now() >= exp_time - timedelta(minutes=minutes)
    except:
        return True


async def auto_refresh_token_for_user(user_id: int) -> bool:
    """自动刷新用户的 WPS token，返回是否成功"""
    from auth.db import get_wps_token, save_wps_token

    token_data = get_wps_token(user_id)
    if not token_data or not token_data.get("refresh_token"):
        return False

    expires_at = token_data.get("expires_at", "2000-01-01")

    # 如果还有 5 分钟以上就不刷新
    if not is_token_expiring_soon(expires_at, minutes=5):
        return True

    try:
        result = await refresh_access_token(token_data["refresh_token"])
        new_access_token = result.get("access_token")
        new_refresh_token = result.get("refresh_token", token_data["refresh_token"])
        new_expires_at = calc_expires_at(result.get("expires_in", 7200))

        # 保存新 token
        save_wps_token(
            user_id,
            new_access_token,
            new_refresh_token,
            new_expires_at,
            token_data.get("wps_user_id", ""),
            token_data.get("wps_username", "")
        )
        return True
    except Exception as e:
        import traceback
        logger.error(f"[WPS TOKEN] auto refresh failed for user {user_id}: {e}\n{traceback.format_exc()}")
        return False


# ── App Token（client_credentials，用于机器人发消息等 app 级操作）──

_app_token_cache: dict = {}  # {"token": str, "expires_at": datetime}


async def get_app_token() -> str:
    """获取应用级 App Token（有效期2小时，内存缓存）"""
    global _app_token_cache
    if _app_token_cache.get("token") and datetime.now() < _app_token_cache.get("expires_at", datetime.min):
        return _app_token_cache["token"]
    async with httpx.AsyncClient() as client:
        resp = await client.post(WPS_TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_id": get_app_id(),
            "client_secret": get_app_secret(),
        })
        resp.raise_for_status()
        data = resp.json()
    token = data.get("access_token") or data.get("token")
    expires_in = data.get("expires_in", 7200)
    _app_token_cache = {
        "token": token,
        "expires_at": datetime.now() + timedelta(seconds=expires_in - 120),
    }
    return token
