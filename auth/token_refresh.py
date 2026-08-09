"""
WPS Token 被动刷新 - 当 API 返回 401 时触发
"""
import logging
from auth.wps_oauth import auto_refresh_token_for_user

logger = logging.getLogger(__name__)


async def try_refresh_on_401(user_id: int) -> bool:
    """
    当 WPS API 返回 401 时调用此函数尝试刷新 token
    返回 True 表示刷新成功，False 表示失败（需要用户重新授权）
    """
    logger.info(f"[WPS TOKEN] Attempting refresh for user {user_id} due to 401")
    success = await auto_refresh_token_for_user(user_id)
    if success:
        logger.info(f"[WPS TOKEN] Refresh successful for user {user_id}")
    else:
        logger.warning(f"[WPS TOKEN] Refresh failed for user {user_id}, user needs to re-authorize")
    return success

