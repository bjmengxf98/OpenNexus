"""
企业微信消息推送客户端
使用群机器人 Webhook 发送消息，无需 IP 白名单和域名验证
"""
import httpx


async def send_wecom_webhook(webhook_url: str, text: str,
                              mention_userid: str = None) -> dict:
    """通过企业微信群机器人 Webhook 发送消息
    mention_userid: 要 @ 的企业微信 userid，None 则不 @
    """
    try:
        mentioned = [mention_userid] if mention_userid else []
        body = {
            "msgtype": "text",
            "text": {
                "content": text,
                "mentioned_list": mentioned,
            }
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(webhook_url, json=body)
            data = resp.json()
        if data.get("errcode", 0) != 0:
            return {"error": f"发送失败: {data.get('errmsg', data)}"}
        print(f"[WECOM WEBHOOK] → @{mention_userid}: {text[:80]}")
        return {"ok": True, "message": f"企业微信消息已发送{'并@' + mention_userid if mention_userid else ''}"}
    except Exception as e:
        return {"error": str(e)}
