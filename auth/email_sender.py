"""
邮件发送模块 - 163 SMTP SSL
"""
import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formataddr

SMTP_SERVER = "smtp.163.com"
SMTP_PORT = 465
SMTP_SERVER = os.environ.get("SMTP_SERVER", SMTP_SERVER)
SMTP_PORT = int(os.environ.get("SMTP_PORT", str(SMTP_PORT)))
SENDER_EMAIL = os.environ.get("SMTP_SENDER_EMAIL", "").strip()
SENDER_PASSWORD = os.environ.get("SMTP_SENDER_PASSWORD", "").strip()
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")
SENDER_NAME = "多维表格智能助手"


def send_email(to_email: str, subject: str, html_body: str) -> tuple[bool, str]:
    if not SENDER_EMAIL or not SENDER_PASSWORD:
        return False, "SMTP 未配置，请设置 SMTP_SENDER_EMAIL 和 SMTP_SENDER_PASSWORD"
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr((str(Header(SENDER_NAME, "utf-8")), SENDER_EMAIL))
        msg["To"] = to_email
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, to_email, msg.as_string())
        return True, "发送成功"
    except Exception as e:
        return False, str(e)


def send_verify_email(to_email: str, username: str, token: str, base_url: str | None = None):
    base_url = (base_url or APP_BASE_URL).rstrip("/")
    link = f"{base_url}/verify?token={token}"
    html = f"""
    <div style="font-family:'Microsoft YaHei',sans-serif;max-width:500px;margin:0 auto;padding:32px">
      <h2 style="color:#0055ff;margin-bottom:8px">多维表格智能助手</h2>
      <p style="color:#666;margin-bottom:24px">请验证您的邮箱以完成注册</p>
      <p>你好，<strong>{username}</strong>！</p>
      <p style="margin:16px 0">点击下方按钮验证邮箱：</p>
      <a href="{link}" style="display:inline-block;background:#0055ff;color:#fff;
        padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600">
        验证邮箱
      </a>
      <p style="color:#999;font-size:12px;margin-top:24px">链接24小时内有效。如非本人操作请忽略。</p>
    </div>"""
    return send_email(to_email, "【多维表格智能助手】邮箱验证", html)


def send_reset_email(to_email: str, username: str, token: str, base_url: str | None = None):
    base_url = (base_url or APP_BASE_URL).rstrip("/")
    link = f"{base_url}/reset?token={token}"
    html = f"""
    <div style="font-family:'Microsoft YaHei',sans-serif;max-width:500px;margin:0 auto;padding:32px">
      <h2 style="color:#0055ff;margin-bottom:8px">多维表格智能助手</h2>
      <p style="color:#666;margin-bottom:24px">密码重置请求</p>
      <p>你好，<strong>{username}</strong>！</p>
      <p style="margin:16px 0">点击下方按钮重置密码：</p>
      <a href="{link}" style="display:inline-block;background:#0055ff;color:#fff;
        padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600">
        重置密码
      </a>
      <p style="color:#999;font-size:12px;margin-top:24px">链接24小时内有效。如非本人操作请忽略。</p>
    </div>"""
    return send_email(to_email, "【多维表格智能助手】密码重置", html)
