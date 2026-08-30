"""
主入口 - FastAPI + 独立 HTML 前端
"""
import asyncio
import tempfile
import os
import secrets
import subprocess
import sys
import time
import warnings
from pathlib import Path
from contextlib import asynccontextmanager

# 自动加载 .env 文件（如果存在）。
#
# 部署时 .env 是 OpenNexus 的权威配置。不能用 setdefault：由 nohup、
# systemd 或旧 shell 遗留的 WPS_REDIRECT_URI 否则会压过 .env，导致
# 生产环境误用 localhost 回调地址。
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ[_k.strip()] = _v.strip()
from fastapi import FastAPI, Request, UploadFile, Form
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse
from core import upload_queue as _uq
from core.wechat_supervisor import WechatRestartSupervisor

# ── 上传 iframe HTML ────────────────────────────────────────

_CHAT_UPLOAD_HTML = """\
<!DOCTYPE html><html><head><meta charset="utf-8"><style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Microsoft YaHei',sans-serif}
label{display:block;border:2px dashed #c4b5fd;border-radius:8px;
  padding:9px 14px;text-align:center;color:#7c3aed;font-size:13px;
  cursor:pointer;background:#faf5ff;transition:background .15s;user-select:none}
label:hover,label.drag{background:#ede9fe;border-color:#7c3aed}
input{display:none}
</style></head><body>
<label for="f" id="lbl">&#x1F4CE; 点击选择文件，或拖拽到此处（Word / PDF / Excel / 图片 / MD）</label>
<input type="file" id="f" multiple
  accept=".txt,.md,.docx,.pdf,.xlsx,.xls,.png,.jpg,.jpeg,.webp">
<script>
var ICONS={'.pdf':'📄','.docx':'📝','.doc':'📝','.xlsx':'📊','.xls':'📊',
  '.png':'🖼️','.jpg':'🖼️','.jpeg':'🖼️','.webp':'🖼️','.txt':'📃','.md':'📃'};
function getIcon(name){var ext=name.slice(name.lastIndexOf('.')).toLowerCase();return ICONS[ext]||'📎';}
function upload(file){
  var fd=new FormData();fd.append('file',file);
  fetch('/api/upload_temp',{method:'POST',body:fd})
    .then(function(r){return r.json();})
    .then(function(d){
      if(d.ok) window.parent.postMessage({type:'file_uploaded',name:d.name,icon:d.icon},'*');
    }).catch(function(){});
}
document.getElementById('f').addEventListener('change',function(){
  Array.from(this.files).forEach(upload);this.value='';
});
var lbl=document.getElementById('lbl');
lbl.addEventListener('dragover',function(e){e.preventDefault();lbl.classList.add('drag');});
lbl.addEventListener('dragleave',function(){lbl.classList.remove('drag');});
lbl.addEventListener('drop',function(e){
  e.preventDefault();lbl.classList.remove('drag');
  Array.from(e.dataTransfer.files).forEach(upload);
});
</script></body></html>
"""

_KB_UPLOAD_HTML = """\
<!DOCTYPE html><html><head><meta charset="utf-8"><style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Microsoft YaHei',sans-serif;background:transparent}
label{display:block;border:2px dashed rgba(99,102,241,.4);border-radius:8px;
  padding:18px;text-align:center;background:rgba(99,102,241,.05);
  cursor:pointer;color:#a5b4fc;font-size:14px;transition:background .15s;user-select:none}
label:hover{background:rgba(99,102,241,.12)}
input{display:none}
#st{font-size:13px;color:#8892a4;min-height:20px;margin-top:8px}
</style></head><body>
<label for="f">&#x1F4C1; 点击选择文件（Word / PDF / Excel / TXT / MD）</label>
<input type="file" id="f" accept=".docx,.pdf,.xlsx,.xls,.txt,.md">
<div id="st"></div>
<script>
document.getElementById('f').addEventListener('change',function(){
  var file=this.files[0];if(!file)return;
  var st=document.getElementById('st');
  st.style.color='#8892a4';st.textContent='⏳ 正在上传：'+file.name+'…';
  var fd=new FormData();fd.append('file',file);
  fetch('/api/admin/kb/upload',{method:'POST',body:fd})
    .then(function(r){return r.json();})
    .then(function(d){
      if(d.ok){
        st.style.color='#22c55e';
        st.textContent='✅ 已添加《'+d.title+'》（'+d.chars.toLocaleString()+' 字）';
        setTimeout(function(){window.parent.location.reload();},1500);
      }else{
        st.style.color='#ef4444';
        st.textContent='❌ 失败：'+(d.error||'未知错误');
      }
    }).catch(function(){st.style.color='#ef4444';st.textContent='❌ 网络错误';});
});
</script></body></html>
"""

from auth import db
from auth.wps_oauth import (
    exchange_code, pop_state, get_wps_user_info, calc_expires_at,
    auto_refresh_token_for_user, is_token_expired
)
from core.mcp_server import mcp_server, mcp_http_app
from core.reminder_text import format_reminder_push_text
from core.wechat_delivery import deliver_personal_weixin, probe_personal_weixin_bridge
from core.tool_governance import normalize_requested_scopes, scope_options

# ── PWA 图标生成 ───────────────────────────────────────────

_APP_DIR = Path(__file__).parent  # app.py 所在目录，无论从哪里启动都正确

def _generate_pwa_icons():
    """首次启动时生成 PWA 图标，需要 Pillow"""
    static_dir = _APP_DIR / "static"
    static_dir.mkdir(exist_ok=True)
    targets = [
        (static_dir / "icon-192.png", 192),
        (static_dir / "icon-512.png", 512),
        (static_dir / "icon-maskable-512.png", 512),
        (static_dir / "apple-touch-icon.png", 180),
    ]
    if all(p.exists() for p, _ in targets):
        return
    try:
        from PIL import Image, ImageDraw, ImageFont
        def make_icon(size):
            img = Image.new("RGB", (size, size))
            draw = ImageDraw.Draw(img)
            # 蓝紫渐变背景
            for y in range(size):
                t = y / size
                r = int(96 * (1 - t) + 79 * t)
                g = int(165 * (1 - t) + 70 * t)
                b = int(250 * (1 - t) + 229 * t)
                draw.line([(0, y), (size - 1, y)], fill=(r, g, b))
            # 白色 ✦ 符号居中
            symbol = "NX"
            font_size = int(size * 0.36)
            font = None
            for fp in ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                       "C:/Windows/Fonts/arial.ttf",
                       "/System/Library/Fonts/Helvetica.ttc"]:
                if Path(fp).exists():
                    try:
                        font = ImageFont.truetype(fp, font_size)
                        break
                    except Exception:
                        pass
            if font is None:
                font = ImageFont.load_default()
            bbox = draw.textbbox((0, 0), symbol, font=font)
            x = (size - (bbox[2] - bbox[0])) // 2 - bbox[0]
            y = (size - (bbox[3] - bbox[1])) // 2 - bbox[1]
            draw.text((x, y), symbol, fill=(255, 255, 255), font=font)
            return img
        for path, size in targets:
            if not path.exists():
                make_icon(size).save(str(path))
        print("[PWA] 图标生成完成")
    except ImportError:
        print("[PWA] 提示：安装 Pillow 可自动生成图标：pip install pillow")
    except Exception as e:
        print(f"[PWA] 图标生成失败：{e}")

_generate_pwa_icons()

# 初始化数据库
db.init_db()

# 清除旧格式提醒记录（event_at 为空，content 里嵌了硬编码日期，会推送错误内容）
_legacy_deleted = db.cleanup_legacy_reminders()
if _legacy_deleted:
    print(f"[STARTUP] 已清除 {_legacy_deleted} 条旧格式提醒记录，请重新设置提醒。")

# 首次运行仅使用显式环境配置创建管理员，避免公开部署使用已知默认密码。
def ensure_admin():
    users = db.list_users()
    if not users:
        admin_email = os.environ.get("INITIAL_ADMIN_EMAIL", "").strip()
        admin_password = os.environ.get("INITIAL_ADMIN_PASSWORD", "").strip()
        admin_username = os.environ.get("INITIAL_ADMIN_USERNAME", "admin").strip() or "admin"
        if admin_email and admin_password:
            db.create_user(admin_username, admin_email, admin_password, is_admin=True)
            print(f"[STARTUP] 已创建初始管理员：{admin_email}")
        else:
            warnings.warn(
                "数据库尚无用户，且未配置 INITIAL_ADMIN_EMAIL/INITIAL_ADMIN_PASSWORD；"
                "请在 .env 中设置后重启以创建初始管理员。",
                RuntimeWarning,
            )

ensure_admin()

_wechat_proc: subprocess.Popen | None = None
_wechat_procs: list = []  # 多账号进程列表
_wechat_port_map: dict = {}  # weixin_id -> port
_wechat_proc_map: dict = {}  # weixin_id -> proc


def _launch_wechat_bridge(node_main: Path, wechat_dir: Path, account_id: str,
                          port: int, show_log: bool = False):
    """启动可选微信桥接，并保留有限大小的诊断日志。"""
    data_dir = str(Path.home() / ".wechat-claude-code" / "instances" / account_id)
    log_path = None
    log_handle = None
    if not show_log:
        log_dir = _APP_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        safe_account = "".join(
            ch if ch.isalnum() or ch in "-_" else "_" for ch in account_id
        )
        log_path = log_dir / f"wechat-bridge-{safe_account}.log"
        try:
            if log_path.exists() and log_path.stat().st_size > 2 * 1024 * 1024:
                rotated = log_path.with_suffix(log_path.suffix + ".1")
                if rotated.exists():
                    rotated.unlink()
                log_path.replace(rotated)
        except OSError as exc:
            print(f"[WeChat] 桥接日志轮转失败，将继续启动: {exc}")
        log_handle = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            ["node", str(node_main), "--account", account_id, "--port", str(port)],
            cwd=str(wechat_dir),
            stdout=None if show_log else log_handle,
            stderr=None if show_log else subprocess.STDOUT,
            env={**os.environ, "WCC_DATA_DIR": data_dir},
        )
    finally:
        if log_handle is not None:
            log_handle.close()
    return proc, log_path

# webhook 去重：{dedup_key: timestamp}，60秒内同一事件只处理一次
_webhook_dedup: dict = {}

# 合规检查防抖：{file_id:rec_id -> asyncio.Task}，同一记录 2 分钟内有新事件则重置
_pending_checks: dict = {}

# 合规检查唤醒事件：{file_id:rec_id -> asyncio.Event}，用户切换记录时立即触发检查
_check_events: dict = {}

# 已发过格式引导的记录：{file_id:rec_id}，每条记录只发一次第一次推送
_guided_records: set = set()

# 跨 webhook 事件积累的字段：{file_id:rec_id -> 累积字段 dict}，供 2 分钟防抖时使用完整字段
_record_fields: dict = {}

# 顺序推送：每行的"第二条推送已完成"事件 {check_key -> asyncio.Event}
_row_completions: dict = {}
# 最近一行的完成事件（新行的第一条推送需等待此事件后再发）
_last_row_completion = None

# WPS 通讯录缓存：name -> user_id（字母格式），1 小时刷新一次
_contacts_cache: dict = {}
_contacts_cache_ts: float = 0.0

# 各文件的字段 ID→名称映射缓存，供 _deferred_check 拉取完整记录时解码字段名
_file_field_maps: dict = {}  # file_id -> {field_id: field_name}

# 各文件的推送成员名单缓存（从"知识推送名单" sheet 读取），1小时刷新
_file_push_members: dict = {}       # file_id -> set of names
_file_push_members_ts: dict = {}    # file_id -> last refresh timestamp

def _csv_env_set(name: str) -> frozenset[str]:
    """Read a private comma-separated allowlist without embedding user data in source."""
    return frozenset(item.strip() for item in os.environ.get(name, "").split(",") if item.strip())


# Optional global compliance-push allowlists. A file-specific "知识推送名单"
# still takes precedence. Keep names and WPS account IDs in .env, never in Git.
_DEPT_MEMBER_NAMES: frozenset[str] = _csv_env_set("COMPLIANCE_MEMBER_NAMES")
_DEPT_MEMBER_IDS: frozenset[str] = _csv_env_set("COMPLIANCE_MEMBER_IDS")

@asynccontextmanager
async def _lifespan(app_):
    # MCP Streamable HTTP 的 session manager 必须与主应用同生命周期运行。
    _mcp_lifecycle = mcp_server.session_manager.run()
    await _mcp_lifecycle.__aenter__()
    global _wechat_proc, _wechat_procs, _wechat_port_map, _wechat_proc_map
    show_log = os.environ.get("WECHAT_SHOW_LOG", "0") == "1"
    _wechat_restart_supervisor = WechatRestartSupervisor(
        base_delay=30, max_delay=600, max_failures=5, stable_seconds=300,
    )
    wechat_dir = _APP_DIR / "wechat-claude-code-main"
    if wechat_dir.exists():
        node_main = wechat_dir / "dist" / "main.js"
        if node_main.exists():
            try:
                import json as _json
                accounts_dir = Path.home() / ".wechat-claude-code" / "accounts"
                account_files = list(accounts_dir.glob("*.json")) if accounts_dir.exists() else []

                # 按 userId 去重，同一个 userId 只保留最新文件，删掉旧的
                from collections import defaultdict
                uid_files: dict = defaultdict(list)
                for af in account_files:
                    try:
                        data = _json.loads(af.read_text(encoding="utf-8"))
                        uid_wx = data.get("userId", "")
                        uid_files[uid_wx].append(af)
                    except Exception:
                        uid_files[""].append(af)
                deduped = []
                for uid_wx, flist in uid_files.items():
                    newest = max(flist, key=lambda x: x.stat().st_mtime)
                    for f in flist:
                        if f != newest:
                            f.unlink()
                    deduped.append((uid_wx, newest))
                # 按 userId 排序，保证每次重启端口分配一致
                deduped.sort(key=lambda x: x[0])

                # 验证 token 是否有效（仅检查，不删文件，避免重启后误删）
                import httpx as _httpx
                valid_deduped = []
                for uid_wx, af in deduped:
                    token_ok = True
                    try:
                        acc = _json.loads(af.read_text(encoding="utf-8"))
                        token = acc.get("botToken", "")
                        async with _httpx.AsyncClient(timeout=8) as _c:
                            _r = await _c.get(
                                "https://ilinkai.weixin.qq.com/ilink/bot/get_updates",
                                headers={"Authorization": f"Bearer {token}", "AuthorizationType": "ilink_bot_token"},
                                params={"get_updates_buf": ""},
                            )
                            _data = _r.json()
                            if _data.get("ret") == -14:
                                # token 过期但保留文件，用户可在设置页重新扫码覆盖
                                print(f"[WeChat] token 已过期，需重新扫码绑定: {af.stem}")
                                token_ok = False
                    except Exception:
                        pass
                    if token_ok:
                        valid_deduped.append((uid_wx, af))
                deduped = valid_deduped

                if deduped:
                    # 先杀掉占用 3001+ 端口的残留 node 进程
                    import socket as _sock
                    for _p in range(3001, 3001 + len(deduped)):
                        try:
                            _s = _sock.create_connection(("127.0.0.1", _p), timeout=0.3)
                            _s.close()
                            # 端口被占，找到对应 pid 并杀掉
                            import psutil as _psutil
                            for _proc_info in _psutil.process_iter(['pid', 'name']):
                                try:
                                    for _conn in _proc_info.connections():
                                        if _conn.laddr.port == _p:
                                            _proc_info.terminate()
                                            break
                                except Exception:
                                    pass
                        except Exception:
                            pass
                    port = 3001
                    for uid_wx, af in deduped:
                        account_id = af.stem
                        proc, bridge_log = _launch_wechat_bridge(
                            node_main, wechat_dir, account_id, port, show_log,
                        )
                        _wechat_procs.append(proc)
                        if uid_wx:
                            _wechat_port_map[uid_wx] = port
                            _wechat_proc_map[uid_wx] = proc
                            _wechat_restart_supervisor.record_started(uid_wx, proc.pid)
                        _log_hint = f", log={bridge_log}" if bridge_log else ""
                        print(f"[WeChat] 已启动账号 {account_id} (pid={proc.pid}, port={port}{_log_hint})")
                        port += 1
                    _wechat_proc = _wechat_procs[0]
                else:
                    print("[WeChat] 未找到账号文件，请在设置页扫码绑定")
            except Exception as e:
                print(f"[WeChat] 启动失败: {e}")
        else:
            print(f"[WeChat] 未找到 {node_main}，跳过（请先 npm run build）")

    # WPS Token 刷新改为被动模式：当 API 返回 401 时自动触发
    # 无需后台定时任务

    # ── WeChat Node daemon 监视和自动重启线程 ──
    _wechat_monitor_stop = False
    def _monitor_wechat_processes():
        """监视可选 Node 桥接；退避重试，连续失败后熔断但不影响主系统。"""
        import time as _time
        import json as _json
        while not _wechat_monitor_stop:
            try:
                _time.sleep(30)  # 每 30 秒检查一次
                for uid_wx, proc in list(_wechat_proc_map.items()):
                    exit_code = proc.poll()
                    if exit_code is None:
                        continue
                    decision = _wechat_restart_supervisor.observe_exit(
                        uid_wx, proc.pid, exit_code,
                    )
                    if decision.new_event:
                        if decision.action == "disabled":
                            print(
                                f"[WeChat] 桥接连续退出 {decision.failures} 次，"
                                f"已暂停自动重启: {uid_wx} (exit={exit_code})。"
                                "个人微信为可选功能，主系统继续运行；请重新扫码或检查桥接日志后重启主服务。"
                            )
                        else:
                            print(
                                f"[WeChat] 桥接已退出: {uid_wx} (exit={exit_code})，"
                                f"{decision.delay_seconds} 秒后尝试第 {decision.failures} 次恢复"
                            )
                    if decision.action != "restart":
                        continue
                    try:
                        accounts_dir = Path.home() / ".wechat-claude-code" / "accounts"
                        account_files = list(accounts_dir.glob("*.json")) if accounts_dir.exists() else []
                        matched_file = None
                        for af in account_files:
                            try:
                                data = _json.loads(af.read_text(encoding="utf-8"))
                            except Exception:
                                continue
                            if data.get("userId") == uid_wx:
                                matched_file = af
                                break
                        node_main = _APP_DIR / "wechat-claude-code-main" / "dist" / "main.js"
                        if matched_file is None or not node_main.exists():
                            raise FileNotFoundError("账号文件或微信桥接程序不存在")
                        account_id = matched_file.stem
                        port = _wechat_port_map.get(uid_wx, 3001)
                        new_proc, bridge_log = _launch_wechat_bridge(
                            node_main,
                            _APP_DIR / "wechat-claude-code-main",
                            account_id,
                            port,
                            show_log,
                        )
                        _wechat_proc_map[uid_wx] = new_proc
                        try:
                            _wechat_procs.remove(proc)
                        except ValueError:
                            pass
                        _wechat_procs.append(new_proc)
                        _wechat_restart_supervisor.record_started(uid_wx, new_proc.pid)
                        _log_hint = f", log={bridge_log}" if bridge_log else ""
                        print(
                            f"[WeChat] 已重启账号 {account_id} "
                            f"(新 pid={new_proc.pid}, port={port}{_log_hint})"
                        )
                    except Exception as e:
                        failed = _wechat_restart_supervisor.record_launch_failure(uid_wx)
                        if failed.action == "disabled":
                            print(
                                f"[WeChat] 重启失败并已熔断: {uid_wx}: {e}。"
                                "个人微信为可选功能，主系统继续运行。"
                            )
                        else:
                            print(
                                f"[WeChat] 重启失败: {uid_wx}: {e}；"
                                f"{failed.delay_seconds} 秒后重试"
                            )
            except Exception as e:
                print(f"[WeChat] 监视线程异常: {e}")

    import threading as _threading
    _monitor_thread = _threading.Thread(target=_monitor_wechat_processes, daemon=True)
    _monitor_thread.start()

    # 提醒调度器：每分钟扫描到期提醒，推送后立即删除
    async def _reminder_scheduler():
        from auth.db import (
            get_due_reminders as _get_due,
            delete_reminder as _del_reminder,
            mark_reminder_failed as _mark_failed,
            log_reminder_delivery as _log_delivery,
        )
        first_scan = True
        while True:
            if first_scan:
                first_scan = False
            else:
                await asyncio.sleep(60)
            try:
                due = _get_due()
                if not due:
                    continue
                print(f"[REMINDER] found {len(due)} due reminder(s) at Beijing time")
                local_token = db.get_system_config("weixin_bot_token", "")
                for row in due:
                    rid = row["id"]
                    content = row["content"]
                    remind_at = row["remind_at"]
                    weixin_id = row.get("personal_weixin_id") or row.get("weixin_id") or ""
                    display = row.get("display_name") or ""

                    event_at = row.get("event_at", "")
                    push_text = format_reminder_push_text(content, event_at, remind_at)
                    pushed = False
                    delivery_channel = ""
                    push_errors = []

                    # 优先：个人微信推送。不能只信任进程内端口映射：服务器
                    # 重启、代理环境变量或端口变化后，映射可能为空或过期。
                    # 每次通过 /health 找到与目标微信完全匹配的桥接，并要求
                    # /local/send 明确返回 ok=true 才视为送达。
                    if weixin_id:
                        _delivery = await deliver_personal_weixin(
                            weixin_id,
                            push_text,
                            local_token,
                            _wechat_port_map,
                        )
                        if _delivery.get("ok") is True:
                            pushed = True
                            delivery_channel = "wechat"
                            print(
                                f"[REMINDER] pushed via WeChat -> "
                                f"{display}({weixin_id}) rid={rid} "
                                f"port={_delivery.get('port')}"
                            )
                        else:
                            _detail = str(
                                _delivery.get("error") or "unknown bridge error"
                            )
                            push_errors.append(f"WeChat: {_detail}")
                            print(
                                f"[REMINDER] WeChat push failed rid={rid}: {_detail}"
                            )

                    # 未绑定个人微信时才兜底到 WPS。
                    # 已绑定微信但桥接临时失败时保留记录重试，不能用 WPS 成功冒充微信送达。
                    if not pushed and not weixin_id:
                        try:
                            pushed = bool(await _push_compliance_notice(display, push_text))
                            if pushed:
                                delivery_channel = "wps"
                                print(f"[REMINDER] pushed via WPS bot -> {display} rid={rid}")
                            else:
                                push_errors.append("WPS bot: no confirmed delivery")
                        except Exception as _e:
                            push_errors.append(f"WPS bot: {_e}")
                            print(f"[REMINDER] WPS push failed rid={rid}: {_e}")

                    # 只有确认送达才删除；失败记录退避重试，避免静默丢失。
                    if pushed:
                        _log_delivery(
                            rid, row["user_id"], remind_at, event_at,
                            delivery_channel, weixin_id or display,
                            "success", "bridge confirmed ok=true",
                        )
                        _del_reminder(rid)
                    else:
                        error_text = "; ".join(push_errors) or "no active delivery channel"
                        _mark_failed(rid, error_text)
                        _log_delivery(
                            rid, row["user_id"], remind_at, event_at,
                            "wechat" if weixin_id else "wps",
                            weixin_id or display, "failed", error_text,
                        )
                        print(f"[REMINDER] rid={rid} delivery failed, retained for retry: {error_text}")
            except Exception as _ex:
                print(f"[REMINDER] scheduler error: {_ex}")

    async def _dashboard_snapshot_scheduler():
        """每天收尾时保存日报；跨日后再补一次，避免关机时间导致漏生成。"""
        from datetime import date as _date, datetime as _datetime, timedelta as _timedelta
        from core.dashboard_service import generate_daily_for_all_users as _generate_daily

        completed_keys: set[str] = set()
        while True:
            await asyncio.sleep(60)
            try:
                now = _datetime.now()
                target = None
                slot = ""
                if now.hour == 23 and now.minute >= 55:
                    target, slot = _date.today(), "close"
                elif now.hour == 0 and 5 <= now.minute < 20:
                    target, slot = _date.today() - _timedelta(days=1), "catchup"
                if target:
                    key = f"{target.isoformat()}:{slot}"
                    if key not in completed_keys:
                        result = await _generate_daily(target)
                        completed_keys.add(key)
                        print(f"[DASHBOARD] daily snapshot {target}: {result}")
                        completed_keys = {item for item in completed_keys if item[:10] >= (target - _timedelta(days=2)).isoformat()}
            except Exception as exc:
                print(f"[DASHBOARD] snapshot scheduler error: {exc}")

    async def _dashboard_cache_scheduler():
        """后台同步 WPS 到 SQLite；网页请求不再承担远程读取耗时。"""
        from core.dashboard_cache import sync_dashboard_cache as _sync_dashboard

        await asyncio.sleep(8)
        cycle = 0
        while True:
            try:
                for user in db.list_users():
                    if not user.get("is_enabled"):
                        continue
                    # 多账号环境下，未授权 WPS 的账号不参与缓存同步。
                    # 不能让一个未连接账号中断其他已连接账号的本轮同步。
                    token_row = db.get_wps_token(user["id"])
                    if not token_row or not token_row.get("access_token"):
                        continue
                    default_file = db.get_default_wps_file(user["id"])
                    if not default_file:
                        continue
                    try:
                        file_id = default_file["file_id"]
                        cache_ready = bool(db.get_dashboard_data_cache(user["id"], file_id, "daily"))
                        # 首次启动全量预热；之后每5分钟增量日报，每30分钟刷新任务/项目/人员。
                        full = (not cache_ready) or cycle % 6 == 0
                        result = await _sync_dashboard(user["id"], file_id, full=full)
                        print(f"[DASHBOARD] cache sync user={user['id']} full={full}: {result}")
                    except Exception as exc:
                        # 单个用户授权过期或 WPS 网络异常时，继续同步后续用户。
                        print(f"[DASHBOARD] cache sync user={user['id']} failed: {exc}")
            except Exception as exc:
                print(f"[DASHBOARD] cache scheduler error: {exc}")
            cycle += 1
            await asyncio.sleep(300)

    from api.app_new_routes import reap_stale_agent_turns
    background_tasks = [
        asyncio.create_task(
            _reminder_scheduler(), name="reminder-scheduler",
        ),
        asyncio.create_task(
            _dashboard_snapshot_scheduler(), name="dashboard-snapshot-scheduler",
        ),
        asyncio.create_task(
            _dashboard_cache_scheduler(), name="dashboard-cache-scheduler",
        ),
        asyncio.create_task(
            reap_stale_agent_turns(), name="agent-turn-reaper",
        ),
    ]

    def _report_background_failure(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            print(f"[BACKGROUND] {task.get_name()} stopped unexpectedly: {error!r}")

    for task in background_tasks:
        task.add_done_callback(_report_background_failure)
    print("[REMINDER] scheduler started and retained by application lifespan")

    try:
        yield
    finally:
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        _wechat_monitor_stop = True
        for proc in _wechat_procs:
            proc.terminate()
        if _wechat_procs:
            print("[WeChat] 微信桥接已停止")
        await _mcp_lifecycle.__aexit__(None, None, None)

fastapi_app = FastAPI(lifespan=_lifespan)
_session_secret = os.environ.get("SESSION_SECRET", "").strip()
if not _session_secret:
    _session_secret = secrets.token_urlsafe(48)
    warnings.warn(
        "SESSION_SECRET 未配置，本次进程将使用临时随机密钥；重启后现有登录会话会失效。",
        RuntimeWarning,
    )
fastapi_app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    max_age=30 * 24 * 3600,
    same_site="lax",
    https_only=os.environ.get("SESSION_COOKIE_SECURE", "0").lower() in {"1", "true", "yes"},
)  # 30天持久登录

@fastapi_app.middleware("http")
async def add_content_language(request, call_next):
    response = await call_next(request)
    response.headers["Content-Language"] = "zh-CN"
    return response


class FixHtmlLangMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if "text/html" in response.headers.get("content-type", ""):
            body = b""
            async for chunk in response.body_iterator:
                body += chunk
            body = body.replace(b'<html lang="en"', b'<html lang="zh-CN" translate="no"')
            headers = dict(response.headers)
            headers["content-length"] = str(len(body))
            headers["Content-Language"] = "zh-CN"
            return StarletteResponse(
                content=body,
                status_code=response.status_code,
                headers=headers,
                media_type="text/html; charset=utf-8",
            )
        return response


fastapi_app.add_middleware(FixHtmlLangMiddleware)
fastapi_app.mount("/static", StaticFiles(directory=str(_APP_DIR / "static")), name="static")
fastapi_app.mount("/mcp", mcp_http_app, name="mcp")


# ── 工具函数 ───────────────────────────────────────────────

def get_current_user(request: Request):
    uid = request.session.get("uid")
    if not uid:
        return None
    return db.get_user_by_id(uid)


@fastapi_app.get("/api/mcp/tokens")
async def api_list_mcp_tokens(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    return JSONResponse({"ok": True, "tokens": db.list_mcp_tokens(user["id"])})


@fastapi_app.post("/api/mcp/tokens")
async def api_create_mcp_token(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    payload = await request.json()
    name = str(payload.get("name") or "WorkBuddy").strip()[:80]
    expires_days = payload.get("expires_days")
    if expires_days not in (None, ""):
        try:
            expires_days = max(1, min(int(expires_days), 3650))
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "有效天数必须是整数"}, status_code=400)
    else:
        expires_days = None
    try:
        scopes = normalize_requested_scopes(payload.get("scopes"))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    created = db.create_mcp_token(user["id"], name, expires_days, scopes)
    return JSONResponse({"ok": True, **created})


@fastapi_app.delete("/api/mcp/tokens/{token_id}")
async def api_revoke_mcp_token(request: Request, token_id: int):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    if not db.revoke_mcp_token(user["id"], token_id):
        return JSONResponse({"ok": False, "error": "令牌不存在或已撤销"}, status_code=404)
    return JSONResponse({"ok": True})


@fastapi_app.get("/api/mcp/info")
async def api_mcp_info(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    endpoint = str(request.base_url).rstrip("/") + "/mcp/"
    return JSONResponse({
        "ok": True,
        "name": "OpenNexus 部门智能管理助手",
        "transport": "Streamable HTTP",
        "endpoint": endpoint,
        "authorization": "Bearer <在设置页创建的 MCP 令牌>",
        "tool_count": len(mcp_server._tool_manager.list_tools()),
        "scope_options": scope_options(),
    })


# ── REST API ───────────────────────────────────────────────

@fastapi_app.post("/api/login")
async def api_login(request: Request):
    data = await request.json()
    user = db.verify_password(data.get("email", ""), data.get("password", ""))
    if not user:
        return JSONResponse({"ok": False, "msg": "邮箱或密码错误，或账号已被禁用"})
    request.session["uid"] = user["id"]
    request.session["is_admin"] = bool(user["is_admin"])
    return JSONResponse({"ok": True})


@fastapi_app.post("/api/register")
async def api_register(request: Request):
    data = await request.json()
    username     = data.get("username", "").strip()
    email        = data.get("email", "").strip()
    password     = data.get("password", "")
    real_name    = data.get("real_name", "").strip()
    organization = data.get("organization", "").strip()
    job_title    = data.get("job_title", "").strip()
    purpose      = data.get("purpose", "").strip()
    if not username or not email or not password:
        return JSONResponse({"ok": False, "msg": "请填写所有字段"})
    # 创建账号，默认禁用，等待邮箱验证
    ok, msg = db.create_user(username, email, password, is_enabled=False,
                             display_name=real_name or username,
                             real_name=real_name, organization=organization,
                             job_title=job_title, purpose=purpose)
    if not ok:
        return JSONResponse({"ok": False, "msg": msg})
    user = db.get_user_by_email(email)
    token = db.create_email_verify_token(user["id"])
    base_url = str(request.base_url).rstrip("/")
    from auth.email_sender import send_verify_email
    sent, err = send_verify_email(email, username, token, base_url)
    if not sent:
        db.delete_user(user["id"])
        return JSONResponse({"ok": False, "msg": f"验证邮件发送失败：{err}"})
    return JSONResponse({"ok": True, "msg": "验证邮件已发送，请查收邮箱并点击链接激活账号"})


@fastapi_app.post("/api/change_password")
async def api_change_password(request: Request):
    uid = request.session.get("uid")
    if not uid:
        return JSONResponse({"ok": False, "msg": "未登录"})
    data = await request.json()
    ok, msg = db.change_password(uid, data.get("old_password", ""), data.get("new_password", ""))
    return JSONResponse({"ok": ok, "msg": msg})


@fastapi_app.get("/verify", response_class=HTMLResponse)
async def verify_email(token: str = None):
    if not token:
        html = _verify_result_html("验证失败", "无效的验证链接。", False)
        return HTMLResponse(html)
    ok, msg = db.consume_email_verify_token(token)
    html = _verify_result_html("验证成功！" if ok else "验证失败", msg, ok)
    return HTMLResponse(html)


def _verify_result_html(title: str, msg: str, success: bool) -> str:
    color = "#22c55e" if success else "#ef4444"
    icon = "✅" if success else "❌"
    redirect = '<meta http-equiv="refresh" content="3;url=/login">' if success else ""
    tip = '<p style="color:#6b7280;font-size:13px;margin-top:12px">3秒后自动跳转到登录页…</p>' if success else '<p style="margin-top:16px"><a href="/register" style="color:#3b82f6">重新注册</a></p>'
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">{redirect}
<style>body{{min-height:100vh;background:#0a0e1a;display:flex;align-items:center;justify-content:center;font-family:'Microsoft YaHei',sans-serif}}
.card{{background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:16px;padding:48px 40px;width:400px;text-align:center}}
</style></head><body><div class="card">
<div style="font-size:48px;margin-bottom:16px">{icon}</div>
<h2 style="color:{color};margin-bottom:8px">{title}</h2>
<p style="color:#9ca3af;font-size:15px">{msg}</p>
{tip}
</div></body></html>"""


@fastapi_app.post("/api/forgot")
async def api_forgot(request: Request):
    data = await request.json()
    email = data.get("email", "").strip()
    if not email:
        return JSONResponse({"ok": False, "msg": "请输入邮箱"})
    ok, result = db.create_reset_token(email)
    if not ok:
        return JSONResponse({"ok": False, "msg": result})
    user = db.get_user_by_email(email)
    from auth.email_sender import send_reset_email
    base_url = str(request.base_url).rstrip("/")
    sent, err = send_reset_email(email, user["username"], result, base_url)
    if not sent:
        return JSONResponse({"ok": False, "msg": f"邮件发送失败：{err}"})
    return JSONResponse({"ok": True})


@fastapi_app.post("/api/reset_password")
async def api_reset_password(request: Request):
    data = await request.json()
    token = data.get("token", "").strip()
    new_password = data.get("password", "")
    if not token or not new_password:
        return JSONResponse({"ok": False, "msg": "参数缺失"})
    if len(new_password) < 6:
        return JSONResponse({"ok": False, "msg": "密码至少6位"})
    ok, msg = db.consume_reset_token(token, new_password)
    return JSONResponse({"ok": ok, "msg": msg})


@fastapi_app.get("/api/logout")
async def api_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")


@fastapi_app.post("/api/upload_temp")
async def api_upload_temp(request: Request, file: UploadFile):
    uid = request.session.get("uid")
    if not uid:
        return JSONResponse({"ok": False, "error": "未登录"})
    suffix = Path(file.filename).suffix
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=tempfile.gettempdir())
    tmp.write(await file.read())
    tmp.close()
    _uq.enqueue(uid, file.filename, tmp.name)
    icons = {".pdf": "📄", ".docx": "📝", ".doc": "📝", ".xlsx": "📊", ".xls": "📊",
             ".png": "🖼️", ".jpg": "🖼️", ".jpeg": "🖼️", ".webp": "🖼️",
             ".txt": "📃", ".md": "📃"}
    return JSONResponse({"ok": True, "name": file.filename,
                         "icon": icons.get(suffix.lower(), "📎")})


@fastapi_app.post("/api/clear_uploads")
async def api_clear_uploads(request: Request):
    uid = request.session.get("uid")
    if uid:
        _uq.clear(uid)
    return JSONResponse({"ok": True})


@fastapi_app.post("/api/switch_wps_file")
async def api_switch_wps_file(request: Request):
    uid = request.session.get("uid")
    if not uid:
        return JSONResponse({"ok": False, "error": "未登录"})
    data = await request.json()
    file_id = data.get("file_id", "")
    if not file_id:
        return JSONResponse({"ok": False, "error": "file_id为空"})
    db.set_default_wps_file(uid, file_id)
    return JSONResponse({"ok": True})


@fastapi_app.post("/api/remove_temp")
async def api_remove_temp(request: Request):
    """删除指定的临时文件"""
    uid = request.session.get("uid")
    if not uid:
        return JSONResponse({"ok": False, "error": "未登录"})

    data = await request.json()
    filename = data.get("filename", "")

    if not filename:
        return JSONResponse({"ok": False, "error": "文件名为空"})

    # 从队列中删除
    success = _uq.remove_by_name(uid, filename)

    return JSONResponse({"ok": success})


# ── 多会话历史 API ──────────────────────────────────────────

@fastapi_app.get("/api/conversations")
async def api_list_conversations(request: Request):
    uid = request.session.get("uid")
    if not uid:
        return JSONResponse({"ok": False}, status_code=401)
    convs = db.list_conversations(uid)
    return JSONResponse({"ok": True, "conversations": convs})


@fastapi_app.post("/api/conversations")
async def api_create_conversation(request: Request):
    uid = request.session.get("uid")
    if not uid:
        return JSONResponse({"ok": False}, status_code=401)
    data = await request.json()
    title = data.get("title", "新对话")
    conv_id = db.create_conversation(uid, title)
    from core.state import user_current_conv
    user_current_conv[int(uid)] = conv_id
    return JSONResponse({"ok": True, "id": conv_id, "title": title})


@fastapi_app.delete("/api/conversations/{conv_id}")
async def api_delete_conversation(conv_id: int, request: Request):
    uid = request.session.get("uid")
    if not uid:
        return JSONResponse({"ok": False}, status_code=401)
    try:
        db.delete_conversation(conv_id, uid)
    except PermissionError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    return JSONResponse({"ok": True})


@fastapi_app.patch("/api/conversations/{conv_id}/title")
async def api_rename_conversation(conv_id: int, request: Request):
    uid = request.session.get("uid")
    if not uid:
        return JSONResponse({"ok": False}, status_code=401)
    data = await request.json()
    title = (data.get("title") or "").strip()
    if not title:
        return JSONResponse({"ok": False, "msg": "标题不能为空"})
    db.rename_conversation(conv_id, uid, title)
    return JSONResponse({"ok": True})


@fastapi_app.get("/api/conversations/{conv_id}/messages")
async def api_get_conversation_messages(conv_id: int, request: Request):
    uid = request.session.get("uid")
    if not uid:
        return JSONResponse({"ok": False}, status_code=401)
    conv = db.get_conversation(conv_id, uid)
    if not conv:
        return JSONResponse({"ok": False, "msg": "会话不存在"}, status_code=404)
    from core.state import user_current_conv
    user_current_conv[int(uid)] = conv_id
    msgs = db.get_chat_history(uid, conv_id=conv_id, limit=50)
    from core.chat_html import user_bubble, ai_bubble, format_timestamp
    bubbles = []
    prev_ts = None
    for msg in msgs:
        ts = msg.get("created_at") or ""
        ts_html = format_timestamp(ts, prev_ts)
        if ts_html:
            bubbles.append(ts_html)
        prev_ts = ts
        if msg["role"] == "user":
            bubbles.append(user_bubble(msg["content"]))
        else:
            bubbles.append(ai_bubble(msg["content"]))

    if not bubbles:
        # 空对话：生成与首次加载一致的欢迎词
        user = db.get_user_by_id(uid)
        default_file = db.get_default_wps_file(uid)
        file_tip = (
            f"已配置默认表格：<b>{default_file.get('file_name') or default_file['file_id']}</b>"
            if default_file else "请先在<b>设置</b>中添加多维表格链接"
        )
        name = (user.get("display_name") or user.get("username", "")) if user else ""
        welcome = (
            f"你好，{name}！我是你的多维表格智能助手。\n\n"
            "你可以：\n"
            "- 直接说'查看任务列表'、'帮我更新任务进度'等指令\n"
            "- 拖拽上传 Word / PDF / 图片，我会自动提取内容写入表格\n"
            "- 查询任务、人员、进度等信息\n\n"
            f"已配置：{file_tip}\n\n"
            "请先在右上角**连接WPS**，并在**设置**中配置大模型 API Key。"
        )
        html = ai_bubble(welcome)
    else:
        html = "".join(bubbles)

    return JSONResponse({"ok": True, "html": html})
async def chat_upload_frame(request: Request):
    if not request.session.get("uid"):
        return HTMLResponse("", status_code=403)
    return HTMLResponse(_CHAT_UPLOAD_HTML)


@fastapi_app.get("/kb_upload_frame", response_class=HTMLResponse)
async def kb_upload_frame_route(request: Request):
    uid = request.session.get("uid")
    if not uid:
        return HTMLResponse("", status_code=403)
    user = db.get_user_by_id(uid)
    if not user or not user["is_admin"]:
        return HTMLResponse("", status_code=403)
    return HTMLResponse(_KB_UPLOAD_HTML)


@fastapi_app.post("/api/admin/kb/upload")
async def api_kb_upload(request: Request, file: UploadFile, title: str = Form(""), category: str = Form("规章制度")):
    tmp_path = None
    try:
        uid = request.session.get("uid")
        if not uid:
            return JSONResponse({"ok": False, "error": "未登录"})
        user = db.get_user_by_id(uid)
        if not user or not user["is_admin"]:
            return JSONResponse({"ok": False, "error": "无权限"})
        if not title:
            title = Path(file.filename).stem
        suffix = Path(file.filename).suffix
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp_path = tmp.name
        tmp.write(await file.read())
        tmp.close()
        from core.file_parser import parse_file
        content = parse_file(tmp_path, file.filename, None, None, None)
        if not content or not content.strip():
            return JSONResponse({"ok": False, "error": "文件���容为空，请检查文件"})
        kid, updated = db.upsert_knowledge(title, content.strip(), file.filename, category or "规章制度")
        # 上传后异步嵌入（有嵌入配置时，失败不影响上传）
        asyncio.create_task(_embed_knowledge_doc(kid, content.strip(), title))
        return JSONResponse({"ok": True, "title": title, "chars": len(content), "updated": updated})
    except Exception as ex:
        import traceback
        traceback.print_exc()
        return JSONResponse({"ok": False, "error": str(ex)})
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass




# ── 知识库向量嵌入（RAG）─────────────────────────────────────
# 独立模块：core/knowledge_rag.py + api/kb_embed_routes.py
from core.knowledge_rag import embed_knowledge_doc as _embed_knowledge_doc, embed_texts as _embed_texts
from api.kb_embed_routes import kb_embed_router
fastapi_app.include_router(kb_embed_router)

# 独立 HTML 部门驾驶舱及其 JSON API
from api.dashboard_routes import dashboard_router
fastapi_app.include_router(dashboard_router)

# 独立 HTML 主界面
from api.app_new_routes import app_new_router
fastapi_app.include_router(app_new_router)

# 独立 HTML 设置页
from api.settings_new_routes import settings_new_router
fastapi_app.include_router(settings_new_router)

# 独立 HTML 管理后台
from api.admin_new_routes import admin_new_router
fastapi_app.include_router(admin_new_router)


# ── 知识提炼 API ────────────────────────────────────────────

_distill_state: dict = {"running": False, "added": 0, "msg": "", "cancel": False}


@fastapi_app.post("/api/admin/distill_knowledge")
async def api_distill_knowledge(request: Request):
    """启动后台知识提炼任务，立即返回。"""
    uid = request.session.get("uid")
    # 兼容早期本机管理调用携带 uid（仅允许本机来源）
    if not uid:
        client_host = request.client.host if request.client else ""
        if client_host in ("127.0.0.1", "::1", "localhost"):
            try:
                body = await request.json()
                uid = body.get("uid")
            except Exception:
                uid = None
    user_row = db.get_user_by_id(uid) if uid else None
    if not user_row or not user_row["is_admin"]:
        return JSONResponse({"ok": False, "msg": "无权限"}, status_code=403)
    if _distill_state["running"]:
        return JSONResponse({"ok": False, "msg": "提炼任务正在进行中"})

    token_row = db.get_wps_token(uid)
    if not token_row or not token_row.get("access_token"):
        return JSONResponse({"ok": False, "msg": "请先连接 WPS"})
    llm_row = db.get_llm_key(uid)
    if not llm_row or not llm_row.get("api_key"):
        return JSONResponse({"ok": False, "msg": "请先在设置页配置大模型 API Key"})
    files = db.list_wps_files(uid)
    if not files:
        return JSONResponse({"ok": False, "msg": "未配置 WPS 文件，请先在设置页添加多维表格"})

    asyncio.create_task(_run_distill(uid, token_row["access_token"], llm_row, files))
    return JSONResponse({"ok": True, "msg": "已启动"})


@fastapi_app.get("/api/admin/distill_knowledge/status")
async def api_distill_status(request: Request):
    return JSONResponse(_distill_state)


@fastapi_app.post("/api/admin/distill_knowledge/cancel")
async def api_distill_cancel(request: Request):
    _distill_state["cancel"] = True
    return JSONResponse({"ok": True})


async def _run_distill(uid: int, access_token: str, llm_row: dict, files: list):
    global _distill_state
    from agent.wps_client import get_schema, list_records as wps_list_records
    from openai import AsyncOpenAI

    _distill_state.update({"running": True, "added": 0, "msg": "初始化…", "cancel": False})

    client = AsyncOpenAI(
        api_key=llm_row["api_key"],
        base_url=llm_row.get("base_url") or "https://api.deepseek.com",
    )
    model = llm_row.get("model") or "deepseek-chat"
    if model.endswith("-reasoning"):
        model = model[:-len("-reasoning")]

    REWRITE_PROMPT = """你是填报规范专家。以下是一条真实填报记录（可能不规范）：

{content}

填报规范要求：
- 每日进展：今日动作（过程）+ 今日成果（结果，必填）+ 问题/下一步（可选）
- 任务：任务名称（动词+对象+成果/节点）+ 执行人 + 优先级 + 完成标准
- 项目：项目名称（年度+对象/主题+目标）+ 负责人 + 项目目标 + 交付成果

请将上述记录改写为规范版本。保留真实业务内容，不要编造事实，补全缺失字段。
输出格式（每行一个字段）：
字段名: 建议值
...
【标签】: 标签1, 标签2, 标签3（3个关键词，用于检索）"""

    added = 0
    existing_titles = {k["title"] for k in db.list_knowledge()}

    try:
        for f in files:
            if _distill_state["cancel"]:
                _distill_state["msg"] = f"已停止（已新增 {added} 条）"
                break
            fid = f["file_id"]
            fname = f.get("file_name", fid)
            _distill_state["msg"] = f"读取文件：{fname}…"
            try:
                schema = await get_schema(access_token, fid)
            except Exception as e:
                print(f"[DISTILL] get_schema failed for {fid}: {e}")
                continue

            for sheet in schema.get("sheets", []):
                if _distill_state["cancel"]:
                    break
                sheet_id = sheet.get("id")
                sheet_name = sheet.get("name", "")
                fields_meta = sheet.get("fields", [])
                if not sheet_id or not fields_meta:
                    continue
                field_names = [fm.get("name", "") for fm in fields_meta]
                is_daily = any(n in field_names for n in ["今日动作", "今日成果", "填报日期"])
                is_task = any(n in field_names for n in ["任务名称", "任务执行人", "完成标准"])
                is_project = any(n in field_names for n in ["项目名称", "项目目标", "交付成果"])
                if not (is_daily or is_task or is_project):
                    continue
                record_type = "每日进展" if is_daily else ("任务" if is_task else "项目")
                _distill_state["msg"] = f"处理：{fname} / {sheet_name}（{record_type}）…"
                try:
                    result = await wps_list_records(access_token, fid, sheet_id, page_size=50)
                    records = result.get("records", [])
                except Exception as e:
                    print(f"[DISTILL] list_records failed {fid}/{sheet_id}: {e}")
                    continue

                for rec in records:
                    if _distill_state["cancel"]:
                        break
                    raw_fields = rec.get("fields", {})
                    if isinstance(raw_fields, str):
                        import json as _json
                        try:
                            raw_fields = _json.loads(raw_fields)
                        except Exception:
                            continue
                    nonempty = {k: v for k, v in raw_fields.items()
                                if v not in (None, "", [], {}) and str(v).strip()}
                    if len(nonempty) < 2:
                        continue
                    content_lines = "\n".join(f"{k}: {v}" for k, v in nonempty.items())
                    try:
                        resp = await client.chat.completions.create(
                            model=model,
                            messages=[{"role": "user", "content": REWRITE_PROMPT.format(content=content_lines)}],
                            max_tokens=300, temperature=0.2,
                        )
                        rewritten = resp.choices[0].message.content.strip()
                    except Exception as e:
                        print(f"[DISTILL] AI rewrite failed: {e}")
                        continue
                    tags = ""
                    lines_out = []
                    for line in rewritten.splitlines():
                        if line.startswith("【标签】"):
                            tags = line.split(":", 1)[-1].strip()
                        else:
                            lines_out.append(line)
                    example_text = "\n".join(lines_out).strip()
                    title = f"{record_type}填报示例-{fname}-{sheet_name}"
                    base_title = title
                    idx = 1
                    while title in existing_titles:
                        title = f"{base_title}({idx})"
                        idx += 1
                    content = (
                        f"【{record_type}填报示例】来源：{fname} / {sheet_name}\n\n"
                        f"{example_text}\n\n关键词：{tags}"
                    )
                    db.add_knowledge(title, content, category="填报示例")
                    existing_titles.add(title)
                    added += 1
                    _distill_state["added"] = added
                    _distill_state["msg"] = f"已提炼 {added} 条，处理中：{sheet_name}…"

        if not _distill_state["cancel"]:
            _distill_state["msg"] = f"完成，共新增 {added} 条知识"
    except Exception as e:
        _distill_state["msg"] = f"出错：{e}"
        print(f"[DISTILL] error: {e}")
    finally:
        _distill_state["running"] = False


# ── 一次性清理 WPS Webhook ──────────────────────────────────

@fastapi_app.post("/api/admin/cleanup_hooks")
async def api_cleanup_hooks(request: Request):
    """清理所有用户在 WPS 上残留的 webhook 订阅（一次性使用）"""
    uid = request.session.get("uid")
    if not uid:
        return JSONResponse({"ok": False, "error": "未登录"})
    user = db.get_user_by_id(uid)
    if not user or not user["is_admin"]:
        return JSONResponse({"ok": False, "error": "无权限，仅管理员可用"})

    from agent.wps_client import list_hooks, delete_hook
    from auth.db import get_wps_token

    results = []
    all_users = db.list_users()
    for u in all_users:
        token_info = get_wps_token(u["id"])
        if not token_info or not token_info.get("access_token"):
            continue
        access_token = token_info["access_token"]
        files = db.list_wps_files(u["id"])
        for f in files:
            fid = f["file_id"]
            try:
                existing = await list_hooks(access_token, fid)
                hooks_raw = existing.get("hooks") or {}
                hook_ids = list(hooks_raw.keys()) if isinstance(hooks_raw, dict) else []
                deleted = 0
                failed = 0
                for hid in hook_ids:
                    try:
                        await delete_hook(access_token, fid, str(hid))
                        deleted += 1
                    except Exception:
                        failed += 1
                results.append({
                    "user": u["email"],
                    "file_id": fid,
                    "total": len(hook_ids),
                    "deleted": deleted,
                    "failed": failed,
                })
            except Exception as e:
                results.append({
                    "user": u["email"],
                    "file_id": fid,
                    "error": str(e),
                })

    total_deleted = sum(r.get("deleted", 0) for r in results)
    return JSONResponse({"ok": True, "total_deleted": total_deleted, "details": results})


# ── WPS OAuth 回调 ─────────────────────────────────────────

@fastapi_app.get("/oauth/callback")
async def oauth_callback(code: str = None, state: str = None, error: str = None):
    if error or not code:
        return RedirectResponse("/?wps_error=1")

    user_id = pop_state(state)
    if not user_id:
        return RedirectResponse("/?wps_error=2")

    try:
        token_data = await exchange_code(code)
        access_token = token_data["access_token"]
        refresh_token = token_data.get("refresh_token", "")
        expires_at = calc_expires_at(token_data.get("expires_in", 7200))

        # 获取 WPS 用户 ID（/v7/users/current_id，需要 kso.user_current_id.read）
        from agent.wps_client import _get as _wps_get
        wps_uid = ""
        wps_name = "已连接"
        try:
            id_result = await _wps_get(access_token, "/v7/users/current_id", signed=True)
            wps_uid = id_result.get("data", {}).get("user_id", "")
        except Exception as e:
            print(f"[WPS OAUTH] get user_id failed: {e}")
        db.save_wps_token(
            user_id, access_token, refresh_token, expires_at,
            wps_uid, wps_name,
        )

        asyncio.create_task(_ensure_webhooks(user_id, access_token))

        return RedirectResponse("/?wps_connected=1")
    except Exception as e:
        print(f"OAuth error: {e}")
        return RedirectResponse("/?wps_error=3")


# ── WPS Webhook 回调 ───────────────────────────────────────

@fastapi_app.get("/wecom/callback")
async def wecom_callback_verify(request: Request):
    """企业微信接收消息服务器URL验证（明文模式）
    企业微信发送 GET 请求，参数含 echostr，原样返回即可通过验证
    """
    echostr = request.query_params.get("echostr", "")
    return HTMLResponse(content=echostr)


@fastapi_app.post("/wecom/callback")
async def wecom_callback_post(request: Request):
    """企业微信消息回调（暂不处理，返回 success）"""
    return HTMLResponse(content="success")


async def _ensure_webhooks(uid: int, access_token: str):
    """OAuth 授权或 token 刷新后，补订阅所有文件所有 sheet 的 webhook"""
    from agent.wps_client import list_hooks, create_hook, get_schema
    webhook_url = os.environ.get("WPS_WEBHOOK_URL", "")
    if not webhook_url:
        print("[WEBHOOK] WPS_WEBHOOK_URL 未配置，跳过补订阅")
        return
    files = db.list_wps_files(uid)
    commands = ["create_record", "update_sheet", "remove_record"]
    for f in files:
        fid = f["file_id"]
        try:
            # 获取该文件所有 sheet_id
            schema = await get_schema(access_token, fid)
            sheet_ids = [s["id"] for s in schema.get("sheets", []) if s.get("id")]
            if not sheet_ids:
                print(f"[WEBHOOK] no sheets found for file {fid}, skip")
                continue

            # 查已有订阅，按 (sheet_id, command) 去重
            existing = await list_hooks(access_token, fid)
            hooks_raw = existing.get("hooks") or {}
            existing_keys: set = set()
            raw_list = hooks_raw.values() if isinstance(hooks_raw, dict) else (hooks_raw if isinstance(hooks_raw, list) else [])
            for h in raw_list:
                sid = str(h.get("sheet_id", ""))
                cmd = h.get("command", "")
                if sid and cmd:
                    existing_keys.add((sid, cmd))

            for sid in sheet_ids:
                for cmd in commands:
                    key = (str(sid), cmd)
                    if key not in existing_keys:
                        await create_hook(access_token, fid, cmd, webhook_url,
                                          data={"sheet_id": sid})
                        print(f"[WEBHOOK] subscribed {cmd} sheet={sid} file={fid}")
        except Exception as e:
            print(f"[WEBHOOK] ensure_webhooks failed for file {fid}: {e}")


def _check_record_compliance(action: str, fields: dict, origin_fields: dict, sheet_name: str = "") -> list:
    """
    检查记录是否符合填报指南，返回问题列表。
    action: createRecord / updateSheet / removeRecord
    fields: 当前字段（已翻译为字段名）
    origin_fields: 修改前字段（updateSheet时有效）
    返回: [问题描述字符串, ...]
    """
    if action == "removeRecord":
        return []

    issues = []
    f = {k.strip(): v for k, v in fields.items()}

    def _nonempty(val) -> bool:
        if val is None:
            return False
        if isinstance(val, str):
            return bool(val.strip())
        if isinstance(val, list):
            return bool(val)
        return True

    # ── 每日进展合规检查 ──────────────────────────────────────
    # 识别标志：sheet_name 含"进展"，或有"今日动作"/"今日成果"等字段
    is_daily = "进展" in sheet_name or any(k in f for k in ["今日动作", "今日成果", "填报日期", "关联任务"])
    if is_daily:
        if not _nonempty(f.get("今日成果")):
            issues.append("今日成果未填写（必填项，不能只写过程动作，需写明形成了什么结果或数量）")
        if not _nonempty(f.get("关联任务")):
            issues.append("未关联任务（每条进展必须关联一个主任务）")
        # 检查进展内容质量
        progress = str(f.get("进展内容", "") or f.get("今日动作", "")).strip()
        if progress:
            _action_verbs = [
                "完成", "编制", "审阅", "审核", "召开", "参加", "提交", "整理", "讨论", "研究",
                "起草", "修改", "参与", "组织", "汇报", "沟通", "协调", "处理", "落实", "推进",
                "开展", "分析", "梳理", "总结", "收集", "测试", "部署", "调试", "联系", "确认",
                "检查", "签署", "审批", "发布", "培训", "调研", "对接", "跟进", "核对", "评审",
            ]
            has_verb = any(v in progress for v in _action_verbs)
            if not has_verb or len(progress) < 6:
                issues.append(
                    f"进展内容过于简略或缺少动作描述（当前：「{progress[:20]}」），"
                    f"应写明具体做了什么，如「审阅上会材料，标注3处修改意见」"
                )
        action_val = f.get("今日动作", "")
        if isinstance(action_val, str) and action_val.strip():
            _process_only = ["推进中", "沟通中", "整理中", "跟进中", "处理中", "了解中", "配合中"]
            if any(p in action_val for p in _process_only) and not _nonempty(f.get("今日成果")):
                issues.append(f"今日动作仅描述过程（\"{action_val[:20]}\"），今日成果必须写明实际产出")
        return issues

    # ── 任务合规检查 ──────────────────────────────────────────
    # 识别标志：sheet_name 含"任务"且不含"进展"，或有任务特征字段（含最常见的"任务名称"）
    is_task = ("任务" in sheet_name and "进展" not in sheet_name) or any(k in f for k in ["任务名称", "所属项目", "任务执行人", "任务成果", "完成标准", "任务类型", "任务来源"])
    if is_task:
        # 必填字段检查：用 f.get() 而非 field in f，确保未填字段也能检出
        required_task = {
            "所属项目": "任务必须关联一个项目",
            "任务名称": "任务名称未填写",
            "任务执行人": "任务执行人未指定（一名主执行人）",
            "优先级": "优先级未选择（P0/P1/P2/P3）",
        }
        for field, msg in required_task.items():
            if not _nonempty(f.get(field)):
                issues.append(msg)
        # 完成标准
        cs = f.get("任务成果") or f.get("完成标准") or f.get("任务成果/完成标准")
        if not _nonempty(cs):
            issues.append("未填写完成标准（说明何种成果形成后才可关闭任务）")
        # 命名格式提示
        task_name = str(f.get("任务名称", "")).strip()
        if task_name:
            _verbs = ["完成", "编制", "形成", "确认", "组织", "提交", "发布", "建立", "实施", "整理",
                      "审核", "报送", "收集", "制定", "落实", "推进", "开展", "分析", "处理", "办理"]
            if not any(task_name.startswith(v) for v in _verbs):
                issues.append(f"任务命名建议以动词开头（格式：动词+对象+成果/节点），当前：\"{task_name[:30]}\"")
        return issues

    # ── 项目合规检查 ──────────────────────────────────────────
    # 识别标志：sheet_name 含"项目"，或有项目特征字段（含最常见的"项目名称"）
    is_project = "项目" in sheet_name or any(k in f for k in ["项目名称", "项目目标", "交付成果", "项目负责人", "项目类型", "所属岗位职责"])
    if is_project:
        required_proj = {
            "项目名称": "项目名称未填写",
            "项目类型": "项目类型未选择（专项项目/年度工作包/临时专项/协同支撑项目）",
            "项目负责人": "项目负责人未指定",
            "项目目标": "项目目标未填写（说明最终要达到的目标，不写过程动作）",
            "交付成果": "交付成果/验收标准未填写",
            "当前状态": "当前状态未选择（未启动/进行中/待反馈/阻塞/已完成/已取消）",
        }
        for field, msg in required_proj.items():
            if not _nonempty(f.get(field)):
                issues.append(msg)
        proj_name = str(f.get("项目名称", "")).strip()
        if proj_name:
            _vague = ["相关工作", "其他配合", "配合工作", "集团派发", "日常工作", "其他事项"]
            if any(v in proj_name for v in _vague):
                issues.append(f"项目名称过于模糊（建议格式：年度+对象/主题+工作目标），当前：\"{proj_name[:30]}\"")
        return issues

    return issues


def _build_format_guidance(sheet_name: str, fields: dict, row_num: int = 0) -> str:
    """为新建记录生成填写格式引导（事前，不依赖用户已填内容）。"""
    field_keys = set(fields.keys())
    # 优先用 sheet_name 判断（首次 updateSheet fields 只含单个字段，field_keys 不可靠）
    is_daily = "进展" in sheet_name or any(k in field_keys for k in ["今日动作", "今日成果", "进展内容"])
    is_task = ("任务" in sheet_name and "进展" not in sheet_name) or any(k in field_keys for k in ["任务名称", "任务执行人", "完成标准"])
    is_project = "项目" in sheet_name or any(k in field_keys for k in ["项目名称", "项目目标", "交付成果"])

    row_label = f" · 第{row_num}行" if row_num else ""
    lines = [f"【填报引导】{sheet_name}{row_label}", ""]
    if is_daily:
        lines += [
            "您正在填写新的每日进展，请注意：",
            "📝 进展内容：描述今天做了什么（具体动作/过程）",
            "✅ 今日成果：今天取得了什么结果（必填，不能只写过程）",
            "🔗 关联任务：必须关联一个主任务",
        ]
    elif is_task:
        lines += [
            "您正在新建任务，请注意：",
            "📌 任务名称：动词+对象+成果/节点，如「完成XX方案编制」",
            "🎯 完成标准：明确的验收条件",
            "📅 截止日期：具体日期",
        ]
    elif is_project:
        lines += [
            "您正在新建项目，请注意：",
            "📌 项目名称：年度+对象/主题+目标，如「2026年XX管理办法修编」",
            "🎯 项目目标：明确的交付目标和验收标准",
        ]
    else:
        lines += ["请按规范填写各字段，填写完成后将自动检查。"]
    lines += ["", "填写完成后，新建下一行，即可收到本行填报规范提醒。"]
    return "\n".join(lines)


def _build_compliance_push_text(action: str, fields: dict, issues: list, sheet_name: str, example: str = "", row_num: int = 0) -> str:
    """构建推送给填报人的合规提醒文本"""
    action_label = {"createRecord": "新增了记录", "updateSheet": "修改了记录"}.get(action, "操作了记录")
    row_label = f" · 第{row_num}行" if row_num else ""
    lines = [
        f"【填报规范提醒】{sheet_name}{row_label}",
        f"您{action_label}，发现 {len(issues)} 项待完善：",
        "",
    ]
    for issue in issues:
        lines.append(f"⚠️ {issue}")
    if example:
        lines += ["", "参考示例（基于您填写的内容生成）：", "", example]
    return "\n".join(lines)


async def _generate_compliance_example(fields: dict, issues: list) -> str:
    """根据用户已填内容，用 AI 生成一个规范填写示例。"""
    try:
        from openai import AsyncOpenAI
        llm_row = None
        for u in db.list_users():
            if u.get("is_admin"):
                row = db.get_llm_key(u["id"])
                if row and row.get("api_key"):
                    llm_row = row
                    break
        if not llm_row:
            return ""
        filled = "\n".join(f"  {k}: {v}" for k, v in fields.items() if v not in (None, "", []))
        print(f"[COMPLIANCE EXAMPLE] filled fields: {filled!r}")
        issues_text = "\n".join(f"- {i}" for i in issues)
        prompt = (
            f"用户在表格中填写了以下内容：\n{filled}\n\n"
            f"系统检测到以下填报规范问题：\n{issues_text}\n\n"
            f"请根据用户已填内容，生成一个规范填写示例。\n"
            f"严格要求：\n"
            f"1. 填报人、填报日期等身份/时间字段原样保留\n"
            f"2. 进展内容、今日成果等描述类字段：规范则保留原值；过于简略则在原值基础上补充具体动作和量化结果\n"
            f"3. 缺失的必填字段，根据用户描述的工作内容合理推断补全\n"
            f"4. 直接输出字段名和建议值，每行一个，格式：字段名: 建议值\n"
            f"5. 不超过8行，不要任何解释和标题"
        )
        client = AsyncOpenAI(
            api_key=llm_row["api_key"],
            base_url=llm_row.get("base_url") or "https://api.deepseek.com",
        )
        resp = await client.chat.completions.create(
            model=llm_row.get("model") or "deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=250,
            temperature=0.3,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[COMPLIANCE] generate_example failed: {e}")
        return ""


async def _llm_compliance_check(sheet_name: str, all_fields: list, filled_fields: dict) -> list:
    """
    用 LLM 检查多维表格填报是否规范，返回问题列表（空列表=无问题）。
    all_fields: 该表格所有字段名（来自 schema）
    filled_fields: 用户已填写的字段及值
    """
    try:
        llm_row = None
        for u in db.list_users():
            if u.get("is_admin"):
                row = db.get_llm_key(u["id"])
                if row and row.get("api_key"):
                    llm_row = row
                    break
        if not llm_row:
            return []

        # 查询知识库中与该表格相关的填报规范（优先 RAG 向量检索，回退关键词检索）
        _kb_text = ""
        try:
            _cfg = db.get_embed_config()
            if _cfg:
                # RAG 路径：将查询语义向量化后检索最相关块
                _query = f"{sheet_name} 填报规范 操作指南"
                _qvec = (await _embed_texts([_query], _cfg["api_key"], _cfg["base_url"], _cfg["model"]))[0]
                _rag_items = db.search_knowledge_rag(_qvec, limit=5)
                if _rag_items:
                    _kb_parts = []
                    for _item in _rag_items:
                        _chunk = _item["chunk_text"]
                        if len(_chunk) > 800:
                            _chunk = _chunk[:800] + "…（已截取）"
                        _kb_parts.append(f"【{_item['title']}】\n{_chunk}")
                    _kb_text = "\n\n".join(_kb_parts)
                    print(f"[COMPLIANCE LLM] RAG: {len(_rag_items)} chunks for {sheet_name!r}")
            if not _kb_text:
                # 回退到关键词检索
                _queries = [sheet_name, "多维表格操作指南", "填报规范"]
                _seen_ids: set = set()
                _kb_items: list = []
                for _q in _queries:
                    for _item in db.search_knowledge(_q, limit=3):
                        if _item.get("id") not in _seen_ids:
                            _seen_ids.add(_item.get("id"))
                            _content = _item["content"]
                            if len(_content) > 800:
                                _content = _content[:800] + "…（已截取）"
                            _kb_items.append(f"【{_item['title']}】\n{_content}")
                if _kb_items:
                    _kb_text = "\n\n".join(_kb_items)
                    print(f"[COMPLIANCE LLM] LIKE: {len(_kb_items)} items for {sheet_name!r}")
        except Exception as _ke:
            print(f"[COMPLIANCE LLM] knowledge search failed: {_ke}")

        schema_str = "、".join(all_fields) if all_fields else "（未获取到字段列表）"
        filled_str = "\n".join(
            f"  {k}: {v}" for k, v in filled_fields.items() if v not in (None, "", [])
        ) or "  （暂无已填内容）"
        unfilled = [f for f in all_fields if f not in filled_fields or filled_fields.get(f) in (None, "", [])]
        unfilled_str = "、".join(unfilled) if unfilled else "无"

        kb_section = (
            f"\n## 本单位填报规范（以此为准）\n{_kb_text}\n"
            if _kb_text else ""
        )

        prompt = (
            f"你是部门规范管理助手。\n"
            f"{kb_section}\n"
            f"员工填写的表格：「{sheet_name}」\n"
            f"表格所有字段：{schema_str}\n\n"
            f"员工已填写的内容：\n{filled_str}\n\n"
            f"尚未填写的字段：{unfilled_str}\n\n"
            f"请严格对照上方本单位填报规范（若无规范则按通用标准），判断填写是否规范完整。\n\n"
            f"如果完全合规，只回复「合规」两个字。\n\n"
            f"如果有问题，每行输出一个问题（不超过5条），格式：\n"
            f"- 问题描述（说明缺了什么、为什么必填，以及对内容质量的要求）\n\n"
            f"要求：\n"
            f"1. 紧扣用户实际填写的内容，不要泛泛而谈\n"
            f"2. 内容质量问题也要指出（如「进展内容」只写了动作没写结果）\n"
            f"3. 不要输出任何其他内容，不要标题，不要解释"
        )

        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            api_key=llm_row["api_key"],
            base_url=llm_row.get("base_url") or "https://api.deepseek.com",
        )
        resp = await client.chat.completions.create(
            model=llm_row.get("model") or "deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.3,
        )
        result = resp.choices[0].message.content.strip()
        print(f"[COMPLIANCE LLM] sheet={sheet_name!r} -> {result[:120]!r}")
        if result.strip() in ("合规", "合规。", "合格", "无问题"):
            return []
        # 解析每行问题，去掉行首的 "- " 符号
        issues = [
            line.lstrip("- •*").strip()
            for line in result.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        return [i for i in issues if i]
    except Exception as e:
        print(f"[COMPLIANCE LLM] failed: {e}")
        return []


async def _knowledge_guide_push(
    op: str, op_id: str, sheet_name: str,
    filled_fields: dict, file_id: str, row_num: int = 0
):
    """
    第三条推送：RAG 语义检索相关规章制度/指导书，经 LLM 加工后推送工作知识指导。
    定位为工作教练，告诉员工这项工作怎么做才能做好。
    RAG 相似度低于阈值时静默不发。
    """
    SIMILARITY_THRESHOLD = 0.35
    _AUTO_FIELDS = {"填报人", "填报日期", "联系人", "创建时间", "最后修改时间",
                    "修改时间", "创建人", "创建日期", "最后编辑时间"}

    # 提取用户填写的工作内容字段（排除自动字段）
    work_parts = []
    for k, v in filled_fields.items():
        if k in _AUTO_FIELDS or not v or v in (None, "", []):
            continue
        if isinstance(v, list):
            v = "、".join(str(i) for i in v if i)
        work_parts.append(f"{k}：{v}")
    if not work_parts:
        print(f"[KNOWLEDGE GUIDE] skip: no user content")
        return

    work_description = "\n".join(work_parts)
    query = f"{sheet_name} {work_description[:200]}"

    # RAG 语义检索
    try:
        cfg = db.get_embed_config()
        if not cfg:
            print(f"[KNOWLEDGE GUIDE] skip: no embed config")
            return
        qvec = (await _embed_texts([query], cfg["api_key"], cfg["base_url"], cfg["model"]))[0]
        rag_items = db.search_knowledge_rag(qvec, limit=5)
    except Exception as e:
        print(f"[KNOWLEDGE GUIDE] RAG search failed: {e}")
        return

    # 过滤低相似度结果
    relevant = [r for r in rag_items if r.get("score", 0) >= SIMILARITY_THRESHOLD]
    if not relevant:
        top_score = rag_items[0]["score"] if rag_items else 0
        print(f"[KNOWLEDGE GUIDE] skip: no relevant knowledge (top score={top_score:.3f})")
        return

    # 拼接知识块（每块最多800字）
    kb_parts = []
    for item in relevant:
        chunk = item["chunk_text"]
        if len(chunk) > 800:
            chunk = chunk[:800] + "…（已截取）"
        kb_parts.append(f"【{item['title']}】\n{chunk}")
    kb_text = "\n\n".join(kb_parts)
    print(f"[KNOWLEDGE GUIDE] {len(relevant)} relevant chunks, top score={relevant[0]['score']:.3f}")

    # 获取 LLM 配置
    try:
        llm_row = None
        for u in db.list_users():
            if u.get("is_admin"):
                row = db.get_llm_key(u["id"])
                if row and row.get("api_key"):
                    llm_row = row
                    break
        if not llm_row:
            print(f"[KNOWLEDGE GUIDE] skip: no LLM config")
            return

        row_label = f"第{row_num}行 " if row_num else ""
        prompt = (
            f"你是部门工作指导助手。\n\n"
            f"## 员工当前工作（{row_label}表格：{sheet_name}）\n"
            f"{work_description}\n\n"
            f"## 相关规章制度和工作指南\n"
            f"{kb_text}\n\n"
            f"请结合上方制度和指南，针对员工正在执行的这项具体工作，"
            f"给出3～5条实用的注意事项和操作建议，告诉他怎么做才能把这件事做好。\n"
            f"要求：\n"
            f"1. 紧扣员工填写的具体工作内容，不要泛泛而谈\n"
            f"2. 每条建议具体可操作，说明做什么、怎么做\n"
            f"3. 语言简洁，每条不超过60字\n"
            f"4. 直接输出建议列表，每条以「- 」开头，不要标题，不要解释"
        )

        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            api_key=llm_row["api_key"],
            base_url=llm_row.get("base_url") or "https://api.deepseek.com",
        )
        resp = await client.chat.completions.create(
            model=llm_row.get("model") or "deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.4,
        )
        guide_text = resp.choices[0].message.content.strip()
        if not guide_text:
            print(f"[KNOWLEDGE GUIDE] LLM returned empty")
            return
    except Exception as e:
        print(f"[KNOWLEDGE GUIDE] LLM failed: {e}")
        return

    # 推送
    row_label = f"第{row_num}行 " if row_num else ""
    source_titles = " / ".join(r["title"] for r in relevant[:3])
    push_text = (
        f"【工作指导】{row_label}{sheet_name}\n\n"
        f"{guide_text}\n\n"
        f"（依据：{source_titles}）"
    )
    print(f"[KNOWLEDGE GUIDE] push -> op={op!r} wps_id={op_id!r}")
    await _push_compliance_notice(op, push_text, op_id, file_id)


async def _push_compliance_notice(operator_name: str, push_text: str, operator_wps_id: str = "", file_id: str = ""):
    """向填报人推送 WPS 私信；只有 API 明确成功时返回 True。"""
    from agent.wps_client import send_bot_message as _wps_bot_send
    global _contacts_cache, _contacts_cache_ts

    target_open_id = ""

    # 白名单过滤：优先用文件的"知识推送名单"，没有则回退到全局白名单
    file_members = _file_push_members.get(file_id) if file_id else None
    if file_members is not None:
        # 文件有专属名单
        if operator_name and operator_name not in file_members:
            print(f"[COMPLIANCE PUSH] {operator_name} not in file push list, skip")
            return False
    else:
        # 回退到部署环境的全局白名单；未配置时安全地拒绝推送
        if not _DEPT_MEMBER_NAMES and not _DEPT_MEMBER_IDS:
            print("[COMPLIANCE PUSH] no global allowlist configured, skip")
            return False
        if operator_name and operator_name not in _DEPT_MEMBER_NAMES:
            print(f"[COMPLIANCE PUSH] {operator_name} not in dept whitelist, skip")
            return False
        if not operator_name and operator_wps_id and operator_wps_id not in _DEPT_MEMBER_IDS:
            print(f"[COMPLIANCE PUSH] wps_id={operator_wps_id} not in dept whitelist, skip")
            return False

    # 方式零：webhook 直接带来的数字格式 WPS ID（直接发，最快）
    if operator_wps_id and operator_wps_id in _DEPT_MEMBER_IDS:
        target_open_id = operator_wps_id
        print(f"[COMPLIANCE PUSH] using webhook operator_wps_id: {operator_wps_id} ({operator_name})")

    # 方式一：系统用户表（已完成 WPS OAuth 授权的用户）
    if not target_open_id and operator_name:
        try:
            all_users = db.list_users()
            for u in all_users:
                display = (u.get("display_name") or u.get("username") or "").strip()
                if display == operator_name:
                    uid = u.get("id")
                    wps_token_row = db.get_wps_token(uid) if uid else None
                    target_open_id = (wps_token_row or {}).get("wps_user_id", "") or ""
                    break
        except Exception as e:
            print(f"[COMPLIANCE PUSH] lookup user failed: {e}")

    # 方式二：WPS 通讯录（无需用户注册，按昵称直查 open_id）
    if not target_open_id and operator_name:
        try:
            import time as _time
            if not _contacts_cache or (_time.time() - _contacts_cache_ts > 3600):
                from agent.wps_client import list_contacts as _list_contacts
                result = await _list_contacts()
                _contacts_cache = {
                    c.get("name", "").strip(): c.get("id", "") or c.get("user_id", "")
                    for c in result.get("users", [])
                    if c.get("name") and (c.get("id") or c.get("user_id"))
                }
                _contacts_cache_ts = _time.time()
                print(f"[COMPLIANCE PUSH] contacts cache refreshed: {len(_contacts_cache)} users")
            target_open_id = _contacts_cache.get(operator_name, "")
            if target_open_id:
                print(f"[COMPLIANCE PUSH] found via org contacts: {operator_name} -> {target_open_id}")
        except Exception as e:
            print(f"[COMPLIANCE PUSH] org contacts lookup failed: {e}")

    if target_open_id:
        try:
            result = await _wps_bot_send(target_open_id, push_text)
            if result.get("ok"):
                print(f"[COMPLIANCE PUSH] wps_bot ok -> {operator_name} ({target_open_id})")
                return True
            else:
                print(f"[COMPLIANCE PUSH] wps_bot api error -> {operator_name}: {result}")
                return False
        except Exception as e:
            print(f"[COMPLIANCE PUSH] wps_bot failed -> {operator_name}: {e}")
            return False
    else:
        print(f"[COMPLIANCE PUSH] no channel for {operator_name}, text logged:")
        print(push_text)
        return False


async def _send_webhook_notifications(file_id: str, action: str, records: list, schema_fields: dict):
    """
    根据 webhook 变更内容，向相关人员发送通知（个人微信 + 企业微信 + 邮件）
    schema_fields: {field_id: {"name": str, "type": str}} 字段映射
    """
    import httpx as _httpx
    from auth.email_sender import send_email as _send_email
    from agent.wecom_client import send_wecom_webhook as _wecom_send

    action_label = {"updateSheet": "更新了记录", "createRecord": "新增了记录", "removeRecord": "删除了记录"}.get(action, action)

    # 找到文件名
    file_name = file_id
    try:
        _all_files = db.list_all_wps_files()
        for _f in _all_files:
            if _f.get("file_id") == file_id:
                file_name = _f.get("file_name", file_id)
                break
    except Exception:
        pass

    # 构建通知文本
    notify_lines = [f"📋 表格变更通知", f"文件：{file_name}", f"操作：{action_label}"]
    contact_names = []
    for rec in records[:3]:  # 最多显示3条
        fields = rec.get("fields", {})
        origin = rec.get("originFields", {})
        changed = []
        for fid, val in fields.items():
            fname = schema_fields.get(fid, {}).get("name", fid)
            ftype = schema_fields.get(fid, {}).get("type", "")
            old_val = origin.get(fid, "")
            if val != old_val:
                changed.append(f"  {fname}: {old_val or '(空)'} → {val}")
            # 收集联系人字段的姓名
            if ftype == "Contact" and val:
                if isinstance(val, list):
                    contact_names += [v.get("nickname", "") for v in val if v.get("nickname")]
                elif isinstance(val, str) and val:
                    contact_names.append(val)
        if changed:
            notify_lines.append("变更内容：")
            notify_lines += changed

    notify_text = "\n".join(notify_lines)

    # 收集需要通知的用户（去重）
    notify_users = {}  # username -> user dict

    # 1. 所有 manager
    try:
        all_users = db.list_users()
        for u in all_users:
            if u.get("role") == "manager" and u.get("is_enabled"):
                notify_users[u["username"]] = u
    except Exception as e:
        print(f"[WEBHOOK NOTIFY] list managers failed: {e}")

    # 2. 联系人字段里的人
    for name in contact_names:
        if not name:
            continue
        try:
            u = db.get_user_by_display_name(name)
            if u:
                notify_users[u["username"]] = dict(u)
        except Exception:
            pass

    print(f"[WEBHOOK NOTIFY] notifying {len(notify_users)} users: {list(notify_users.keys())}")

    wecom_webhook_url = db.get_system_config("wecom_webhook_url", "")

    for username, user in notify_users.items():
        uid = user.get("id")
        email = user.get("email", "")

        # 个人微信通知
        personal_wx = db.get_personal_weixin_id(uid) if uid else ""
        if personal_wx:
            try:
                async with _httpx.AsyncClient(timeout=10) as _c:
                    local_token = db.get_system_config("weixin_bot_token", "")
                    port_candidates = list(_wechat_port_map.values()) if _wechat_port_map else [3001]
                    for port in port_candidates:
                        resp = await _c.post(
                            f"http://127.0.0.1:{port}/local/send",
                            json={"to": personal_wx, "text": notify_text, "token": local_token},
                        )
                        if resp.status_code == 200:
                            print(f"[WEBHOOK NOTIFY] weixin ok -> {username}")
                            break
            except Exception as e:
                print(f"[WEBHOOK NOTIFY] weixin failed -> {username}: {e}")

        # 企业微信通知
        wecom_userid = db.get_wecom_userid(username)
        if wecom_webhook_url:
            try:
                await _wecom_send(wecom_webhook_url, notify_text, mention_userid=wecom_userid or None)
                print(f"[WEBHOOK NOTIFY] wecom ok -> {username}")
            except Exception as e:
                print(f"[WEBHOOK NOTIFY] wecom failed -> {username}: {e}")

        # 邮件通知
        if email:
            try:
                import asyncio as _asyncio
                html = f"<pre style='font-family:sans-serif'>{notify_text}</pre>"
                await _asyncio.get_event_loop().run_in_executor(
                    None, lambda: _send_email(email, f"表格变更通知 - {file_name}", html)
                )
                print(f"[WEBHOOK NOTIFY] email ok -> {email}")
            except Exception as e:
                print(f"[WEBHOOK NOTIFY] email failed -> {email}: {e}")


@fastapi_app.post("/wps/webhook")
async def wps_webhook(request: Request):
    """接收 WPS 多维表格 webhook 推送，翻译字段ID后存日志"""
    global _last_row_completion
    import json as _json
    try:
        payload = await request.json()
    except Exception:
        return {"code": 400, "msg": "invalid json"}

    if "challenge" in payload:
        return {"challenge": payload.get("challenge")}

    events = payload.get("events", [])
    file_id = payload.get("file_id", "")
    if not events or not file_id:
        return {"code": 0, "msg": "ok"}

    # 打印原始 payload（前2个事件），用于分析 webhook 到底带了哪些字段
    print(f"[WEBHOOK RAW] file_id={file_id}, events_count={len(events)}")
    for _i, _ev in enumerate(events[:2]):
        print(f"[WEBHOOK RAW] event[{_i}] = {_json.dumps(_ev, ensure_ascii=False)[:500]}")

    # 获取字段 ID → 字段名映射，同时记录 sheet_id → sheet_name 映射
    field_map = {}
    sheet_name_map: dict = {}   # sheet_id (str) -> sheet_name
    sheet_fields_map: dict = {} # sheet_id (str) -> [field_name, ...]
    try:
        _token_row = db.get_any_valid_wps_token()
        if _token_row:
            from agent.wps_client import get_schema as _get_schema, list_records as _list_rec
            _schema = await _get_schema(_token_row["access_token"], file_id)
            _push_sheet_id = None
            _push_field_id = None
            _push_field_name = None
            for _sheet in _schema.get("sheets", []):
                _sid = str(_sheet.get("id", ""))
                _sname = _sheet.get("name", _sid)
                if _sid:
                    sheet_name_map[_sid] = _sname
                    sheet_fields_map[_sid] = [_f.get("name", "") for _f in _sheet.get("fields", []) if _f.get("name")]
                for _f in _sheet.get("fields", []):
                    field_map[_f["id"]] = _f.get("name", _f["id"])
                if _sname == "知识推送名单" and _sid:
                    _push_sheet_id = _sid
                    for _f in _sheet.get("fields", []):
                        if _f.get("name") in ("联系人", "成员", "姓名", "人员"):
                            _push_field_id = _f.get("id")
                            _push_field_name = _f.get("name")
                            break
            _file_field_maps[file_id] = field_map

            # 读取"知识推送名单" sheet，缓存成员名单（1小时刷新一次）
            import time as _ts_time
            _ts_now = _ts_time.time()
            if _push_sheet_id and _push_field_name and (
                file_id not in _file_push_members_ts or
                _ts_now - _file_push_members_ts.get(file_id, 0) > 3600
            ):
                try:
                    _push_result = await _list_rec(
                        _token_row["access_token"], file_id, int(_push_sheet_id), page_size=200
                    )
                    _members: set = set()
                    _raw_recs = _push_result.get("records", [])
                    print(f"[PUSH MEMBERS DEBUG] sheet={_push_sheet_id} field={_push_field_id} recs={len(_raw_recs)}")
                    if _raw_recs:
                        print(f"[PUSH MEMBERS DEBUG] first record fields: {_raw_recs[0].get('fields', {})}")

                    def _pick_name(val) -> str:
                        """从任意 WPS 人员字段格式中提取姓名（支持 list/dict/str）"""
                        if isinstance(val, str):
                            return val.strip()
                        if isinstance(val, list):
                            return _pick_name(val[0]) if val else ""
                        if isinstance(val, dict):
                            return (val.get("nickName") or val.get("name") or
                                    val.get("nickname") or val.get("displayName") or
                                    val.get("text") or val.get("value") or "")
                        return ""

                    for _prec in _raw_recs:
                        # list_records 返回的字段键已是字段名，优先用名字查，再用ID兜底
                        _fval = (_prec.get("fields", {}).get(_push_field_name) or
                                 _prec.get("fields", {}).get(_push_field_id))
                        _n = _pick_name(_fval)
                        if _n:
                            _members.add(_n)
                    _file_push_members[file_id] = _members
                    _file_push_members_ts[file_id] = _ts_now
                    print(f"[PUSH MEMBERS] {file_id} -> {_members}")
                except Exception as _pe:
                    print(f"[PUSH MEMBERS] read failed for {file_id}: {_pe}")
    except Exception as e:
        print(f"[WEBHOOK] get_schema failed: {e}")

    def _translate(fields: dict) -> dict:
        """把字段 ID 替换为字段名"""
        if not field_map:
            return fields
        return {field_map.get(k, k): v for k, v in fields.items()}

    # 去重窗口：5 秒内同一事件视为重复（防 WPS 重发），不影响正常多次编辑
    import time as _time
    _now = _time.time()
    # 清理过期条目（超 10 秒）
    expired_keys = [k for k, ts in _webhook_dedup.items() if _now - ts > 10]
    for k in expired_keys:
        _webhook_dedup.pop(k, None)

    for event in events:
        content = event.get("content", {})
        action = content.get("action", "")
        if not action:
            continue

        # 翻译 records 里的字段 ID
        records = content.get("records", [])
        translated_records = []
        for rec in records:
            translated_records.append({
                "id": rec.get("id", ""),
                "fields": _translate(rec.get("fields", {})),
                "originFields": _translate(rec.get("originFields", {})),
            })
        log_content = {
            "action": action,
            "records": translated_records,
            "source": "webhook",
        }
        db.add_change_log(file_id, action, _json.dumps(log_content, ensure_ascii=False))

        # ── 合规检查 + 主动推送 ────────────────────────────────
        # 只对 createRecord / updateSheet 做合规检查
        if action not in ("createRecord", "updateSheet"):
            continue

        sheet_id = str(content.get("sheet_id") or content.get("sheetId") or "")
        sheet_name = sheet_name_map.get(sheet_id, sheet_id) or "表格"
        all_field_names = sheet_fields_map.get(sheet_id, [])
        print(f"[WEBHOOK] {action}, sheet={sheet_name!r}, fields_schema={all_field_names}, records={len(translated_records)}")

        for rec in translated_records:
            rec_id = rec.get("id", "")
            fields = rec.get("fields", {})
            origin = rec.get("originFields", {})

            # 字段积累必须在 dedup 之前，否则 5 秒内第二次编辑被跳过，字段丢失
            if action == "updateSheet" and rec_id:
                check_key_pre = f"{file_id}:{rec_id}"
                if check_key_pre not in _record_fields:
                    _record_fields[check_key_pre] = {}
                _record_fields[check_key_pre].update(fields)

            # dedup：同一记录 5 秒内只触发一次合规调度
            dedup_key = f"{file_id}:{action}:{rec_id}:{int(_now // 5)}"
            if dedup_key in _webhook_dedup:
                continue
            _webhook_dedup[dedup_key] = _now

            # 提取操作人
            operator_name = ""
            operator_wps_id = ""
            operator_obj = content.get("operator") or event.get("operator") or {}
            if isinstance(operator_obj, dict):
                operator_name = operator_obj.get("nickname") or operator_obj.get("name") or ""
                operator_wps_id = str(operator_obj.get("id") or operator_obj.get("userId") or operator_obj.get("user_id") or "").strip()
            if not operator_name:
                operator_name = event.get("user_name") or event.get("userName") or ""
            if not operator_wps_id:
                operator_wps_id = str(event.get("user_id") or event.get("userId") or "").strip()
            if not operator_name:
                for _fname in ["填报人", "任务执行人", "项目负责人"]:
                    _fval = fields.get(_fname)
                    if isinstance(_fval, list) and _fval:
                        _fval = _fval[0]
                    if isinstance(_fval, dict):
                        operator_name = _fval.get("nickname") or _fval.get("name") or ""
                    elif isinstance(_fval, str):
                        operator_name = _fval
                    if operator_name:
                        break
            # 当前事件字段没有找到操作人时，从跨事件累积字段中兜底提取
            if not operator_name:
                _acc = _record_fields.get(f"{file_id}:{rec_id}", {})
                for _fname in ["填报人", "任务执行人", "项目负责人"]:
                    _fval = _acc.get(_fname)
                    if isinstance(_fval, list) and _fval:
                        _fval = _fval[0]
                    if isinstance(_fval, dict):
                        operator_name = _fval.get("nickname") or _fval.get("name") or ""
                    elif isinstance(_fval, str):
                        operator_name = _fval
                    if operator_name:
                        break
            print(f"[COMPLIANCE] webhook op: name={operator_name!r} wps_id={operator_wps_id!r} sheet={sheet_name!r}")

            check_key = f"{file_id}:{rec_id}"
            accumulated_fields = _record_fields.get(check_key, fields)

            # 用户点开新行的信号：该记录的首次 updateSheet（含自动填入的填报人/日期）
            _is_new_row = action == "updateSheet" and check_key not in _guided_records

            # 第一次推送：该记录首次 updateSheet 时发格式引导（createRecord 无数据，改在此触发）
            if _is_new_row:
                _guided_records.add(check_key)

                # 顺序推送：当前行等上一行的第二条推送完成后再发第一条
                _prev_row_evt = _last_row_completion
                _my_row_evt = asyncio.Event()
                _row_completions[check_key] = _my_row_evt
                _last_row_completion = _my_row_evt

                async def _first_push(_op=operator_name, _op_id=operator_wps_id,
                                      _fid=file_id, _sid=sheet_id, _rid=rec_id,
                                      _sn=sheet_name, _flds=fields,
                                      _prev=_prev_row_evt):
                    if _prev is not None:
                        try:
                            await asyncio.wait_for(_prev.wait(), timeout=180)
                        except asyncio.TimeoutError:
                            print(f"[COMPLIANCE FIRST PUSH] ordering wait timeout, sending anyway")
                        await asyncio.sleep(5)  # 让用户有时间阅读上一行的第二条消息
                    _rnum = 0
                    if _sid and _rid:
                        try:
                            _tok = db.get_any_valid_wps_token()
                            if _tok:
                                from agent.wps_client import list_records as _wps_lr
                                _res = await _wps_lr(_tok["access_token"], _fid, _sid, page_size=1000)
                                for _i, _r in enumerate(_res.get("records", []), 1):
                                    if str(_r.get("id", "")) == str(_rid):
                                        _rnum = _i
                                        break
                            else:
                                print(f"[COMPLIANCE FIRST PUSH] no valid WPS token")
                        except Exception as _ex:
                            print(f"[COMPLIANCE FIRST PUSH] list_records failed: {_ex}")
                    print(f"[COMPLIANCE FIRST PUSH] sid={_sid!r} rid={_rid!r} rnum={_rnum}")
                    guidance = _build_format_guidance(_sn, _flds, row_num=_rnum)
                    await _push_compliance_notice(_op, guidance, _op_id, _fid)

                asyncio.create_task(_first_push())

            # 第二次推送：换格子防抖（15 秒无新编辑则触发），用 LLM 检查填写规范
            async def _deferred_check(
                _op=operator_name, _op_id=operator_wps_id, _act=action, _flds=accumulated_fields,
                _sn=sheet_name, _key=check_key, _all_fields=all_field_names,
                _sid=sheet_id, _rid=rec_id, _event=None, _completion=None
            ):
                try:
                    # 等待：用户切换到其他记录时立即触发（事件驱动）；
                    # 1小时超时只做内存清理，不跑检查——最后一行靠用户新增下一行触发
                    if _event is not None:
                        try:
                            await asyncio.wait_for(_event.wait(), timeout=3600)
                        except asyncio.TimeoutError:
                            _pending_checks.pop(_key, None)
                            if _completion:
                                _completion.set()
                            return
                except asyncio.CancelledError:
                    print(f"[COMPLIANCE] cancelled (reset) for {_key}")
                    return
                _pending_checks.pop(_key, None)
                latest_flds = _record_fields.get(_key, _flds)
                row_num = 0
                print(f"[COMPLIANCE] fired for {_key}, sheet={_sn!r}, op={_op!r}, wps_id={_op_id!r}, fields={list(latest_flds.keys())}")
                # 操作人提取：先从事件时捕获的值，再从积累字段，最后拉取完整记录
                op = _op
                op_id = _op_id

                def _extract_op_from_fields(flds):
                    for _fname in ["填报人", "任务执行人", "项目负责人", "联系人", "操作人"]:
                        _fval = flds.get(_fname)
                        if isinstance(_fval, list) and _fval:
                            _fval = _fval[0]
                        if isinstance(_fval, dict):
                            _n = _fval.get("nickname") or _fval.get("name") or ""
                            if _n:
                                return _n
                        elif isinstance(_fval, str) and _fval.strip():
                            return _fval.strip()
                    return ""

                if not op and not op_id:
                    op = _extract_op_from_fields(latest_flds)

                # 拉取 WPS 完整记录：让 LLM 看到全部字段值（不只是本次改动的字段）
                if _sid and _rid:
                    try:
                        _fid = _key.split(":")[0]
                        _token_row = db.get_any_valid_wps_token()
                        if _token_row:
                            from agent.wps_client import list_records as _wps_fetch
                            _fmap = _file_field_maps.get(_fid, {})
                            _rec_result = await _wps_fetch(_token_row["access_token"], _fid, _sid, page_size=1000)
                            for _ridx, _rec in enumerate(_rec_result.get("records", []), 1):
                                if str(_rec.get("id", "")) == str(_rid):
                                    _full = {_fmap.get(k, k): v for k, v in _rec.get("fields", {}).items()}
                                    latest_flds = {**_full, **latest_flds}  # 完整记录为底，webhook积累为顶
                                    row_num = _ridx
                                    if not op and not op_id:
                                        op = _extract_op_from_fields(latest_flds)
                                    print(f"[COMPLIANCE] fetched full record row={row_num}, fields={list(_full.keys())}, op={op!r}")
                                    break
                    except Exception as _e:
                        print(f"[COMPLIANCE] fetch full record failed: {_e}")

                if not op and not op_id:
                    print(f"[COMPLIANCE] skip {_key}: operator unknown")
                    if _completion:
                        _completion.set()
                    return
                if not any(v not in (None, "", []) for v in latest_flds.values()):
                    print(f"[COMPLIANCE] skip {_key}: all fields empty")
                    if _completion:
                        _completion.set()
                    return
                # 只有系统自动填充字段（填报人/日期/时间戳）有值时，跳过检查
                # 避免用户刚建空行就收到"内容为空"的提醒
                _AUTO_FIELDS = {"填报人", "填报日期", "联系人", "创建时间", "最后修改时间",
                                "修改时间", "创建人", "创建日期", "最后编辑时间"}
                _has_user_content = any(
                    v not in (None, "", [])
                    for k, v in latest_flds.items()
                    if k not in _AUTO_FIELDS
                )
                if not _has_user_content:
                    print(f"[COMPLIANCE] skip {_key}: only auto-filled fields, no user content yet")
                    if _completion:
                        _completion.set()
                    return
                # LLM 合规检查：返回问题列表
                issues = await _llm_compliance_check(_sn, _all_fields, latest_flds)
                if not issues:
                    print(f"[COMPLIANCE] skip {_key}: LLM says compliant")
                    if _completion:
                        _completion.set()
                    # 合规时跳过第二条，但仍推送工作知识指导（第三条）
                    asyncio.create_task(_knowledge_guide_push(
                        op, op_id, _sn, latest_flds,
                        _key.split(":")[0], row_num
                    ))
                    return
                # 根据实际填写内容生成针对性示例
                example = await _generate_compliance_example(latest_flds, issues)
                # 格式化推送文本（与事前推送风格一致）
                push_text = _build_compliance_push_text(_act, latest_flds, issues, _sn, example, row_num=row_num)
                print(f"[COMPLIANCE] push -> op={op!r} wps_id={op_id!r} / {_sn} / {len(issues)} issues")
                await _push_compliance_notice(op, push_text, op_id, _key.split(":")[0])
                if _completion:
                    _completion.set()
                # 第三条：工作知识指导（RAG+LLM，静默条件：相似度不足）
                asyncio.create_task(_knowledge_guide_push(
                    op, op_id, _sn, latest_flds,
                    _key.split(":")[0], row_num
                ))

            # 触发其他记录事后检查的条件（满足其一即可）：
            # 1. 用户点开了新行（首次 updateSheet，含自动填入字段）→ 上一行已离开，可以检查了
            # 2. 用户在当前行主动填写了内容（非系统自动字段）
            _AUTO_FIELDS_TRIGGER = {"填报人", "填报日期", "联系人", "创建时间",
                                    "最后修改时间", "修改时间", "创建人", "创建日期",
                                    "最后编辑时间"}
            _should_fire_others = _is_new_row or (
                action == "updateSheet" and
                any(k not in _AUTO_FIELDS_TRIGGER for k in fields.keys())
            )
            if _should_fire_others:
                # 用户已离开其他记录，立即唤醒所有待检查记录
                for _other_key, _other_ev in list(_check_events.items()):
                    if _other_key.startswith(f"{file_id}:") and _other_key != check_key:
                        print(f"[COMPLIANCE] fire check for {_other_key} (new_row={_is_new_row})")
                        _other_ev.set()
            else:
                print(f"[COMPLIANCE] no user edit detected (action={action!r}, fields={list(fields.keys())}), skip firing others")

            old_task = _pending_checks.pop(check_key, None)
            if old_task and not old_task.done():
                old_task.cancel()
            _check_events.pop(check_key, None)
            _fire_event = asyncio.Event()
            _check_events[check_key] = _fire_event
            _pending_checks[check_key] = asyncio.create_task(
                _deferred_check(_event=_fire_event, _completion=_row_completions.get(check_key))
            )
            print(f"[COMPLIANCE] deferred check scheduled for {check_key} sheet={sheet_name!r} op={operator_name!r}")


    return {"code": 0, "msg": "ok"}


# ── 微信 Bot 接口 ──────────────────────────────────────────

def _md_to_plain(text: str) -> str:
    """把 Markdown 转成微信友好的纯文本（参考 weclaw MarkdownToPlainText）"""
    import re
    # 代码块：去掉围栏，保留内容（先处理，避免被行内规则误伤）
    text = re.sub(r'```[^\n]*\n?(.*?)```', lambda m: m.group(1).strip(), text, flags=re.DOTALL)
    # 图片 ![alt](url) → 去掉
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text)
    # 链接 [text](url) → text
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    # 表格分隔行 |---|---| → 去掉
    text = re.sub(r'(?m)^\|[\s:|\-]+\|$', '', text)
    # 表格行 | a | b | → a  b
    def _table_row(m):
        cells = [c.strip() for c in m.group(1).split('|')]
        return '  '.join(cells)
    text = re.sub(r'(?m)^\|(.+)\|$', _table_row, text)
    # 标题 ## xxx → xxx
    text = re.sub(r'(?m)^#{1,6}\s+', '', text)
    # 粗体 **xxx** / __xxx__
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    # 删除线 ~~xxx~~
    text = re.sub(r'~~(.+?)~~', r'\1', text)
    # 引用 > xxx → xxx
    text = re.sub(r'(?m)^>\s?', '', text)
    # 水平线 → 去掉
    text = re.sub(r'(?m)^[-*_]{3,}\s*$', '', text)
    # 无序列表 - / * / + → • （保留缩进）
    text = re.sub(r'(?m)^(\s*)[-*+]\s+', r'\1• ', text)
    # 行内代码 `xxx` → xxx（在代码块之后处理）
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # 多余空行压缩
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()
# 供本地 Node.js 微信 bot 调用，不需要 session，用 weixin_id 识别用户

_weixin_histories: dict = {}  # weixin_id -> list of messages


@fastapi_app.post("/api/weixin/chat")
async def weixin_chat(request: Request):
    """
    微信 bot 专用接口
    请求体: { "weixin_id": "xxx", "text": "用户消息", "token": "内部密钥" }
    返回:   { "reply": "AI回复" }
    """
    import json as _json
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    # 简单内部密钥验证，防止外部调用
    internal_token = db.get_system_config("weixin_bot_token", "")
    if internal_token and body.get("token") != internal_token:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    weixin_id = body.get("weixin_id", "").strip()
    text = body.get("text", "").strip()
    images = body.get("images", [])  # [{"media_type": "image/png", "data": "base64..."}]
    if not weixin_id or (not text and not images):
        return JSONResponse({"error": "missing weixin_id or text"}, status_code=400)

    # /myid 命令：无需登录，直接返回 weixin_id
    if text == "/myid":
        return JSONResponse({"reply": f"你的微信ID是：{weixin_id}\n请复制后登录系统，在「设置」页填入「个人微信ID」字段保存。"})

    # 查找绑定的系统用户
    user = db.get_user_by_weixin_id(weixin_id)
    if not user:
        return JSONResponse({"reply": "您还未绑定系统账号，请发送 /bind 用户名 密码 进行绑定。"})
    user = dict(user)

    uid = user["id"]
    llm_cfg = db.get_llm_key(uid)
    if not llm_cfg or not llm_cfg.get("api_key"):
        return JSONResponse({"reply": "您的账号未配置 AI 模型，请登录系统后在设置页面配置。"})

    # 获取 WPS token（自动刷新过期 token，刷新后补订阅 webhook）
    # WPS 刷新失败不影响微信聊天，捕获异常后降级为无 token 继续运行
    wps_token_row = db.get_wps_token(uid)
    try:
        if wps_token_row and wps_token_row.get("expires_at"):
            if is_token_expired(wps_token_row["expires_at"]):
                refreshed = await auto_refresh_token_for_user(uid)
                if refreshed:
                    wps_token_row = db.get_wps_token(uid)
                    print(f"[WPS TOKEN] auto refreshed for uid={uid}, re-subscribing webhooks")
                    await _ensure_webhooks(uid, wps_token_row["access_token"])
    except Exception as _wps_err:
        print(f"[WeChat Bot] WPS token refresh failed, continuing without WPS: {_wps_err}")
        wps_token_row = None
    access_token = wps_token_row["access_token"] if wps_token_row else ""

    # 处理图片：识别后追加到 text
    if images:
        print(f"[WeChat] 收到图片数量: {len(images)}")
        image_llm_cfg = db.get_image_llm_key(uid)
        main_vision_cfg = llm_cfg if (llm_cfg.get("advanced") or {}).get("supports_vision") else None
        image_advanced = (image_llm_cfg or {}).get("advanced") or {}
        vision_cfg = main_vision_cfg or (
            image_llm_cfg
            if image_llm_cfg and image_llm_cfg.get("api_key")
            and image_advanced.get("supports_vision", True)
            else None
        )
        print(
            f"[WeChat] 视觉配置来源: "
            f"{'主模型' if main_vision_cfg else ('图片模型' if vision_cfg else '未配置')}"
        )
        if vision_cfg and vision_cfg.get("api_key"):
            import tempfile, os as _os
            from core.file_parser import parse_file
            recognized_parts = []
            for idx, img in enumerate(images):
                media_type = img.get("media_type", "image/png")
                ext = media_type.split("/")[-1].replace("jpeg", "jpg")
                b64_data = img.get("data", "")
                print(f"[WeChat] 图片{idx+1}: media_type={media_type}, data长度={len(b64_data)}")
                if not b64_data:
                    continue
                try:
                    import base64 as _b64
                    raw = _b64.b64decode(b64_data)
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}")
                    tmp.write(raw)
                    tmp.close()
                    recognized = parse_file(
                        tmp.name, f"image.{ext}",
                        api_key=vision_cfg["api_key"],
                        base_url=vision_cfg.get("base_url"),
                        model=vision_cfg.get("model"),
                        max_output_tokens=(vision_cfg.get("advanced") or {}).get("max_output_tokens"),
                    )
                    recognized_parts.append(f"[图片{idx+1}内容]\n{recognized}")
                except Exception as e:
                    recognized_parts.append(f"[图片{idx+1}识别失败: {e}]")
                finally:
                    try:
                        _os.unlink(tmp.name)
                    except Exception:
                        pass
            if recognized_parts:
                text = (text + "\n\n" if text else "") + "\n\n".join(recognized_parts)
        else:
            text = (text + "\n\n" if text else "") + "[收到图片，但未配置图片识别模型，请在设置中配置视觉模型]"

    if not text:
        return JSONResponse({"reply": "收到图片但识别失败，请检查图片识别模型配置。"})

    # 维护对话历史（每个微信用户独立，最多保留 20 条）
    history = _weixin_histories.get(weixin_id, [])
    # 微信不支持 Markdown，注入系统提示要求纯文本回复
    weixin_system = {"role": "system", "content": "你现在通过微信回复用户，微信不支持Markdown格式。请用纯文本回复，不要使用**粗体**、##标题、---分隔线等符号，用空行分段，用「」或序号代替格式标记，保持简洁易读。如果用户消息中包含[图片N内容]标记，说明图片已由视觉模型识别完毕，识别结果就在消息里，请直接基于识别出的文字内容回答，不要说自己看不到图片。"}
    history_with_system = [weixin_system] + history
    history.append({"role": "user", "content": text})
    if len(history) > 20:
        history = history[-20:]

    # 获取用户信息
    default_file = db.get_default_wps_file(uid)
    all_files = db.list_wps_files(uid)
    memory = db.get_user_memory(uid)

    from agent.assistant import Assistant
    assistant = Assistant(
        api_key=llm_cfg["api_key"],
        provider=llm_cfg.get("provider", "deepseek"),
        base_url=llm_cfg.get("base_url"),
        model=llm_cfg.get("model"),
        advanced=llm_cfg.get("advanced"),
    )

    try:
        reply = await assistant.chat(
            messages=history_with_system + [{"role": "user", "content": text}],
            access_token=access_token,
            username=user.get("display_name") or user.get("username"),
            role=user.get("role", "staff"),
            default_file=dict(default_file) if default_file else None,
            all_files=[dict(f) for f in all_files],
            memory=memory,
            uid=uid,
        )
    except Exception as e:
        print(f"[WeChat Bot] chat error: {e}")
        return JSONResponse({"reply": f"处理出错：{e}"})

    # 更新历史
    history.append({"role": "assistant", "content": reply})
    _weixin_histories[weixin_id] = history[-20:]

    return JSONResponse({"reply": _md_to_plain(reply)})


_wechat_setup_proc: subprocess.Popen | None = None
_wechat_qr_url: str = ""  # 缓存最新二维码 URL，供前端渲染
_wechat_qr_base64: str = ""  # 缓存生成的二维码 Base64，供前端直接显示
_wechat_rendered_url: str = ""  # 记录已渲染的 URL，用于检测刷新
_wechat_setup_result: dict | None = None  # 缓存 SETUP_RESULT，供 setup_status 读取
_wechat_setup_started_at: float = 0.0
_wechat_setup_last_error: str = ""

@fastapi_app.get("/api/weixin/qrcode")
async def weixin_qrcode(request: Request):
    """返回微信扫码二维码图片"""
    if not request.session.get("uid"):
        return JSONResponse({"error": "未登录"}, status_code=401)
    qr_path = Path.home() / ".wechat-claude-code" / "qrcode.png"
    if qr_path.exists():
        from fastapi.responses import FileResponse
        return FileResponse(str(qr_path), media_type="image/png")
    return JSONResponse({"error": "二维码不存在"}, status_code=404)


@fastapi_app.post("/api/weixin/start_setup")
async def weixin_start_setup(request: Request):
    """启动微信扫码绑定流程"""
    global _wechat_setup_proc, _wechat_proc, _wechat_procs, _wechat_qr_url
    global _wechat_setup_result, _wechat_setup_started_at, _wechat_setup_last_error
    if not request.session.get("uid"):
        return JSONResponse({"error": "未登录"}, status_code=401)
    wechat_dir = _APP_DIR / "wechat-claude-code-main"
    node_main = wechat_dir / "dist" / "main.js"
    if not node_main.exists():
        return JSONResponse({"error": "微信桥接未构建"}, status_code=400)
    if _wechat_setup_proc and _wechat_setup_proc.poll() is None:
        try:
            _wechat_setup_proc.terminate()
        except Exception:
            pass
    qr_path = Path.home() / ".wechat-claude-code" / "qrcode.png"
    if qr_path.exists():
        qr_path.unlink()
    _wechat_qr_url = ""
    _wechat_qr_base64 = ""
    _wechat_rendered_url = ""
    _wechat_setup_result = None
    _wechat_setup_started_at = time.monotonic()
    _wechat_setup_last_error = ""
    try:
        _wechat_setup_proc = subprocess.Popen(
            ["node", str(node_main), "setup"],
            cwd=str(wechat_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            bufsize=1,
            errors="replace",
        )
    except FileNotFoundError:
        _wechat_setup_proc = None
        return JSONResponse(
            {"error": "服务器未安装 Node.js，无法启动微信桥接"}, status_code=500
        )
    except Exception as exc:
        _wechat_setup_proc = None
        print(f"[wx_setup] start failed: {exc}")
        return JSONResponse({"error": f"微信桥接启动失败：{exc}"}, status_code=500)

    import threading, json as _json
    def _reader():
        global _wechat_qr_url, _wechat_setup_result, _wechat_setup_last_error
        proc = _wechat_setup_proc
        if not proc or not proc.stdout:
            return
        for line in iter(proc.stdout.readline, ''):
            line = line.strip()
            if not line:
                continue
            print(f"[wx_setup] {line!r}")
            if line.startswith("QR_URL:"):
                _wechat_qr_url = line[len("QR_URL:"):]
                _wechat_qr_base64 = ""  # 清空旧图，让前端重新生成
            elif line.startswith("SETUP_RESULT:"):
                try:
                    _wechat_setup_result = _json.loads(line[len("SETUP_RESULT:"):])
                except Exception as e:
                    print(f"[WeChat] 解析 SETUP_RESULT 失败: {e}")
            elif any(word in line.lower() for word in ("error", "failed", "exception", "not found")):
                _wechat_setup_last_error = line[-300:]
    threading.Thread(target=_reader, daemon=True).start()

    return JSONResponse({"ok": True, "message": "扫码流程已启动，请刷新二维码"})


async def _activate_wechat_binding(uid: int, new_account_id: str, new_weixin_id: str) -> tuple[bool, str]:
    """启动并自检新桥接，成功后再原子替换当前用户的旧绑定。"""
    global _wechat_proc, _wechat_procs, _wechat_port_map, _wechat_proc_map
    import socket as _socket
    import httpx as _httpx

    if not new_account_id or not new_weixin_id:
        return False, "未能读取完整账号信息，请重试"

    # 系统账号与个人微信必须一一对应，禁止把已属于其他系统用户的
    # 微信通过重新扫码直接抢占过来。
    existing_owner = db.get_user_by_weixin_id(new_weixin_id)
    if existing_owner and int(dict(existing_owner)["id"]) != int(uid):
        owner_name = dict(existing_owner).get("display_name") or dict(existing_owner).get("username")
        return False, f"该个人微信已绑定系统用户 {owner_name}，不能重复绑定"

    wechat_dir = _APP_DIR / "wechat-claude-code-main"
    node_main = wechat_dir / "dist" / "main.js"
    old_weixin_id = db.get_personal_weixin_id(uid) or ""

    def _port_is_free(port: int) -> bool:
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False

    new_port = 3001
    while new_port <= 3099 and not _port_is_free(new_port):
        new_port += 1
    if new_port > 3099:
        return False, "微信桥接端口已用尽，请重启服务后重试"

    data_dir = str(Path.home() / ".wechat-claude-code" / "instances" / new_account_id)
    proc = subprocess.Popen(
        ["node", str(node_main), "--account", new_account_id, "--port", str(new_port)],
        cwd=str(wechat_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "WCC_DATA_DIR": data_dir},
    )

    bridge_ready = False
    bridge_error = "微信桥接启动超时"
    # 本机自检不能继承 HTTP(S)_PROXY，否则 127.0.0.1 请求可能被代理
    # 截获并返回其他服务的内容，造成“账号自检不一致”的假失败。
    async with _httpx.AsyncClient(timeout=1.5, trust_env=False) as client:
        for _ in range(20):
            if proc.poll() is not None:
                bridge_error = f"微信桥接启动后立即退出（退出码 {proc.returncode}）"
                break
            try:
                health = await client.get(f"http://127.0.0.1:{new_port}/health")
                health_data = health.json() if health.status_code == 200 else {}
                if (health_data.get("ok") is True
                        and health_data.get("accountId") == new_account_id
                        and health_data.get("userId") == new_weixin_id):
                    bridge_ready = True
                    break
                bridge_error = (
                    "微信桥接账号自检不一致"
                    f"（期望 {new_account_id}/{new_weixin_id}，"
                    f"实际 {health_data.get('accountId', '-')}/{health_data.get('userId', '-')}）"
                )
            except Exception:
                pass
            await asyncio.sleep(0.25)

    if not bridge_ready:
        try:
            proc.terminate()
        except Exception:
            pass
        print(f"[WeChat] rebind bridge failed: account={new_account_id} port={new_port} error={bridge_error}")
        return False, f"绑定信息已取得，但{bridge_error}；原连接已保留，请重试"

    # 只有新桥接通过自检后，才关闭当前用户的旧桥接并更新数据库。
    stale_ids = {wxid for wxid in (old_weixin_id, new_weixin_id) if wxid}
    for stale_id in stale_ids:
        old_proc = _wechat_proc_map.get(stale_id)
        if old_proc and old_proc is not proc:
            try:
                old_proc.terminate()
            except Exception:
                pass
            if old_proc in _wechat_procs:
                _wechat_procs.remove(old_proc)
        _wechat_proc_map.pop(stale_id, None)
        _wechat_port_map.pop(stale_id, None)

    _wechat_procs.append(proc)
    _wechat_proc = proc
    _wechat_port_map[new_weixin_id] = new_port
    _wechat_proc_map[new_weixin_id] = proc
    db.set_weixin_id(uid, new_weixin_id)
    db.set_personal_weixin_id(uid, new_weixin_id)

    # 只清理当前用户旧微信或同一微信的历史 bot 凭据。其他用户的
    # 账号文件必须保留。这样每个系统用户最终只有一个活动微信桥接。
    accounts_dir = Path.home() / ".wechat-claude-code" / "accounts"
    if accounts_dir.exists():
        import json as _json
        for account_file in accounts_dir.glob("*.json"):
            if account_file.stem == new_account_id:
                continue
            try:
                account_data = _json.loads(account_file.read_text(encoding="utf-8"))
                account_weixin_id = str(account_data.get("userId") or "")
                if account_weixin_id in {old_weixin_id, new_weixin_id}:
                    account_file.unlink()
                    print(f"[WeChat] removed stale credential: {account_file.name}")
            except Exception as cleanup_error:
                print(f"[WeChat] stale credential cleanup skipped: {account_file.name}: {cleanup_error}")
    print(f"[WeChat] binding activated user={uid} account={new_account_id} weixin={new_weixin_id} port={new_port}")
    return True, "绑定成功，微信桥接已连接并通过自检"


@fastapi_app.get("/api/weixin/setup_status")
async def weixin_setup_status(request: Request):
    """查询扫码绑定状态"""
    global _wechat_setup_proc, _wechat_qr_url, _wechat_setup_started_at, _wechat_setup_last_error
    uid = request.session.get("uid")
    if not uid:
        return JSONResponse({"error": "未登录"}, status_code=401)
    qr_path = Path.home() / ".wechat-claude-code" / "qrcode.png"
    if _wechat_setup_proc:
        ret = _wechat_setup_proc.poll()
        if ret is None:
            if (not qr_path.exists() and _wechat_setup_started_at
                    and time.monotonic() - _wechat_setup_started_at > 45):
                try:
                    _wechat_setup_proc.terminate()
                except Exception:
                    pass
                _wechat_setup_proc = None
                detail = f"（{_wechat_setup_last_error}）" if _wechat_setup_last_error else ""
                return JSONResponse({
                    "status": "failed",
                    "message": (
                        "二维码生成超时。服务器可能无法访问微信二维码接口，"
                        f"或微信桥接依赖异常{detail}；请查看 app.log 中 [wx_setup] 日志。"
                    ),
                })
            return JSONResponse({"status": "waiting", "has_qr": qr_path.exists(), "qr_url": _wechat_qr_url})
        _wechat_setup_proc = None
        if ret != 0:
            detail = f"：{_wechat_setup_last_error}" if _wechat_setup_last_error else "，请查看 app.log 中 [wx_setup] 日志"
            return JSONResponse({"status": "failed", "message": f"绑定失败{detail}"})

        new_account_id = (_wechat_setup_result or {}).get("accountId", "")
        new_weixin_id = (_wechat_setup_result or {}).get("userId", "")
        print(f"[WeChat] setup result: new_account_id={new_account_id!r} new_weixin_id={new_weixin_id!r}")
        ok, message = await _activate_wechat_binding(int(uid), new_account_id, new_weixin_id)
        return JSONResponse({"status": "done" if ok else "failed", "message": message})
    return JSONResponse({"status": "idle"})

def _masked_weixin_id(value: str) -> str:
    value = (value or "").strip()
    if len(value) <= 8:
        return value
    return f"{value[:4]}…{value[-4:]}"


@fastapi_app.get("/api/weixin/status")
async def weixin_delivery_status(request: Request):
    """Return the real bridge status instead of treating a saved ID as online."""
    uid = request.session.get("uid")
    if not uid:
        return JSONResponse({"error": "未登录"}, status_code=401)
    personal_weixin_id = db.get_personal_weixin_id(int(uid)) or ""
    if not personal_weixin_id:
        return JSONResponse({
            "ok": True, "bound": False, "connected": False,
            "message": "尚未绑定个人微信",
        })
    port, error = await probe_personal_weixin_bridge(
        personal_weixin_id, _wechat_port_map,
    )
    connected = port is not None
    return JSONResponse({
        "ok": True,
        "bound": True,
        "connected": connected,
        "personal_weixin_id": _masked_weixin_id(personal_weixin_id),
        "port": port,
        "message": "微信桥接在线，可以接收提醒" if connected else error,
    })


@fastapi_app.post("/api/weixin/test")
async def weixin_delivery_test(request: Request):
    """Send an immediate message so users can verify reminder delivery."""
    uid = request.session.get("uid")
    if not uid:
        return JSONResponse({"error": "未登录"}, status_code=401)
    personal_weixin_id = db.get_personal_weixin_id(int(uid)) or ""
    if not personal_weixin_id:
        return JSONResponse({"error": "尚未绑定个人微信"}, status_code=400)
    local_token = db.get_system_config("weixin_bot_token", "")
    result = await deliver_personal_weixin(
        personal_weixin_id,
        "✅ OpenNexus 微信提醒测试成功。今后的定时提醒会通过此通道发送。",
        local_token,
        _wechat_port_map,
    )
    if result.get("ok") is not True:
        return JSONResponse(
            {"error": f"测试消息发送失败：{result.get('error') or '未知错误'}"},
            status_code=503,
        )
    return JSONResponse({"ok": True, "message": "测试消息已发送，请检查微信"})



@fastapi_app.post("/api/weixin/notify")
async def weixin_notify(request: Request):
    """
    主动向指定用户发送微信消息
    请求体: { "to_user_id": 系统用户ID, "text": "消息内容", "token": "内部密钥" }
    或:     { "to_weixin_id": "微信ID", "text": "消息内容", "token": "内部密钥" }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    internal_token = db.get_system_config("weixin_bot_token", "")
    if internal_token and body.get("token") != internal_token:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    text = body.get("text", "").strip()
    if not text:
        return JSONResponse({"error": "missing text"}, status_code=400)

    # 支持两种方式指定接收人
    to_weixin_id = body.get("to_weixin_id", "").strip()
    if not to_weixin_id:
        to_user_id = body.get("to_user_id")
        if to_user_id:
            to_weixin_id = db.get_personal_weixin_id(int(to_user_id))

    if not to_weixin_id:
        return JSONResponse({"error": "无法找到目标用户的个人微信ID，请先在设置页填写"}, status_code=400)

    # 调用 Node.js 本地发送接口
    import httpx
    local_token = db.get_system_config("weixin_bot_token", "")
    # 按 weixin_id 找对应端口，找不到则轮询所有端口
    port_candidates = []
    if to_weixin_id in _wechat_port_map:
        port_candidates = [_wechat_port_map[to_weixin_id]]
    elif _wechat_port_map:
        port_candidates = list(_wechat_port_map.values())
    else:
        port_candidates = list(range(3001, 3001 + len(_wechat_procs))) if _wechat_procs else [3001]
    print(f"[weixin_notify] to_weixin_id={to_weixin_id!r} port_candidates={port_candidates} port_map={_wechat_port_map}")

    last_err = "无可用微信桥接进程"
    async with httpx.AsyncClient(timeout=10) as client:
        for port in port_candidates:
            try:
                resp = await client.post(
                    f"http://127.0.0.1:{port}/local/send",
                    json={"to": to_weixin_id, "text": text, "token": local_token},
                )
                if resp.status_code == 200:
                    return JSONResponse({"ok": True})
                last_err = f"port {port}: {resp.text}"
            except Exception as e:
                last_err = f"port {port}: {e}"
    return JSONResponse({"error": f"发送失败: {last_err}"}, status_code=500)


@fastapi_app.post("/api/weixin/session_expired")
async def weixin_session_expired(request: Request):
    """node 进程 token 过期时回调，通知对应用户重新扫码"""
    try:
        body = await request.json()
        weixin_id = body.get("userId", "")
        account_id = body.get("accountId", "")
        if not weixin_id:
            return JSONResponse({"ok": True})
        # 从 port_map 里移除过期的映射
        if weixin_id in _wechat_port_map:
            del _wechat_port_map[weixin_id]
        if weixin_id in _wechat_proc_map:
            del _wechat_proc_map[weixin_id]
        # 找到对应的系统用户，给他发一条微信消息提醒
        import httpx as _httpx
        uid_expired = db.get_uid_by_weixin_id(weixin_id) if hasattr(db, "get_uid_by_weixin_id") else None
        if uid_expired:
            # 用另一个还活着的 bot 发通知（轮询所有端口）
            local_token = db.get_system_config("weixin_bot_token", "")
            for port in _wechat_port_map.values():
                try:
                    async with _httpx.AsyncClient(timeout=5) as client:
                        await client.post(
                            f"http://127.0.0.1:{port}/local/send",
                            json={"to": weixin_id, "text": "⚠️ 你的微信连接已过期，请登录系统设置页重新扫码绑定。", "token": local_token},
                        )
                    break
                except Exception:
                    pass
        print(f"[WeChat] token 过期: {account_id} userId={weixin_id}")
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": True})


@fastapi_app.post("/api/weixin/bind")
async def weixin_bind(request: Request):
    """微信用户绑定系统账号"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    weixin_id = body.get("weixin_id", "").strip()
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    if not all([weixin_id, username, password]):
        return JSONResponse({"ok": False, "msg": "参数不完整"})

    # 验证账号密码
    user = db.get_user_by_username(username)
    if not user:
        return JSONResponse({"ok": False, "msg": "用户名不存在"})
    import bcrypt
    if not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        return JSONResponse({"ok": False, "msg": "密码错误"})
    if not user["is_enabled"]:
        return JSONResponse({"ok": False, "msg": "账号已禁用"})

    user = dict(user)
    db.set_weixin_id(user["id"], weixin_id)
    db.set_personal_weixin_id(user["id"], weixin_id)
    return JSONResponse({"ok": True, "msg": f"绑定成功，欢迎 {user.get('display_name') or username}！双向通信已开启。"})


# ── PWA Manifest ───────────────────────────────────────────

@fastapi_app.get("/manifest.json")
async def pwa_manifest():
    return JSONResponse({
        "id": "/",
        "name": "OpenNexus 多维表格智能助手",
        "short_name": "OpenNexus",
        "description": "面向部门事务、任务、项目、知识库和消息提醒的智能工作助手",
        "start_url": "/",
        "scope": "/",
        "lang": "zh-CN",
        "display": "standalone",
        "display_override": ["window-controls-overlay", "standalone", "minimal-ui"],
        "background_color": "#f5f6fa",
        "theme_color": "#4f46e5",
        "orientation": "any",
        "categories": ["business", "productivity", "utilities"],
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
        "shortcuts": [
            {"name": "开始对话", "short_name": "对话", "url": "/", "icons": [{"src": "/static/icon-192.png", "sizes": "192x192"}]},
            {"name": "部门驾驶舱", "short_name": "驾驶舱", "url": "/dashboard", "icons": [{"src": "/static/icon-192.png", "sizes": "192x192"}]},
        ],
    })


@fastapi_app.get("/sw.js", include_in_schema=False)
async def pwa_service_worker():
    return FileResponse(
        _APP_DIR / "static" / "service-worker.js",
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Service-Worker-Allowed": "/"},
    )


@fastapi_app.get("/offline", include_in_schema=False)
async def pwa_offline_page():
    return FileResponse(_APP_DIR / "static" / "offline.html", media_type="text/html; charset=utf-8")


# ── 独立 HTML 认证页面 ─────────────────────────────────────

_AUTH_HTML_FILE = _APP_DIR / "static" / "auth.html"

@fastapi_app.get("/login", response_class=HTMLResponse)
async def page_login():
    return HTMLResponse(_AUTH_HTML_FILE.read_text(encoding="utf-8"))


@fastapi_app.get("/register", response_class=HTMLResponse)
async def page_register():
    return HTMLResponse(_AUTH_HTML_FILE.read_text(encoding="utf-8"))


@fastapi_app.get("/forgot", response_class=HTMLResponse)
async def page_forgot():
    return HTMLResponse(_AUTH_HTML_FILE.read_text(encoding="utf-8"))


@fastapi_app.get("/reset", response_class=HTMLResponse)
async def page_reset():
    return HTMLResponse(_AUTH_HTML_FILE.read_text(encoding="utf-8"))


# ── 启动 ───────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    import threading
    import argparse
    import webbrowser

    # 自动清理 8000 端口
    def kill_port(port):
        try:
            if sys.platform == "win32":
                subprocess.run(f'for /f "tokens=5" %a in (\'netstat -ano ^| findstr :{port}\') do taskkill /F /PID %a',
                             shell=True, capture_output=True)
            else:
                subprocess.run(f"lsof -ti:{port} | xargs kill -9", shell=True, capture_output=True)
        except:
            pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    port = args.port

    # 启动前清理端口
    kill_port(port)

    def open_browser():
        import time
        import urllib.request

        # 启动阶段还要初始化微信、WPS、MCP 等服务。固定等待几秒可能会在
        # Uvicorn 尚未接受请求时过早打开浏览器，进而触发 PWA 离线页。
        login_url = f"http://127.0.0.1:{port}/login"
        for _ in range(120):
            try:
                with urllib.request.urlopen(login_url, timeout=0.5) as response:
                    if response.status == 200:
                        break
            except Exception:
                time.sleep(0.25)
        webbrowser.open(f"http://localhost:{port}/login")
    threading.Thread(target=open_browser, daemon=True).start()
    # Uvicorn 0.40 forces ProactorEventLoop for a single Windows worker.
    # A client that resets during AcceptEx can raise WinError 64 and leave the
    # process alive while permanently closing the listening socket.  This app
    # does not use asyncio subprocess APIs, so SelectorEventLoop is the safer
    # Windows server loop.  Linux keeps Uvicorn's normal automatic selection.
    server_loop = "asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto"
    print(f"[STARTUP] Uvicorn event loop: {server_loop}")
    uvicorn.run("app:fastapi_app", host="0.0.0.0", port=port, reload=False, loop=server_loop)
