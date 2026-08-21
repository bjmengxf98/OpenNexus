"""
数据库模块 - SQLite用户管理
"""
import sqlite3
import json
import bcrypt
import secrets
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "app.db"
BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_now() -> datetime:
    """返回明确的北京时间，不依赖服务器操作系统时区。"""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def get_conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _create_reminder_delivery_log_table(conn) -> None:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS reminder_delivery_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        reminder_id  INTEGER NOT NULL,
        user_id      INTEGER NOT NULL,
        remind_at    TEXT DEFAULT '',
        event_at     TEXT DEFAULT '',
        channel      TEXT DEFAULT '',
        target       TEXT DEFAULT '',
        status       TEXT NOT NULL,
        detail       TEXT DEFAULT '',
        attempted_at TEXT NOT NULL
    )
    """)


def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            is_enabled INTEGER DEFAULT 1,
            role TEXT DEFAULT 'staff',
            department TEXT DEFAULT '',
            display_name TEXT DEFAULT '',
            plan TEXT DEFAULT 'free',
            created_at TEXT DEFAULT (datetime('now')),
            last_login TEXT
        );

        CREATE TABLE IF NOT EXISTS wps_tokens (
            user_id INTEGER PRIMARY KEY,
            access_token TEXT,
            refresh_token TEXT,
            expires_at TEXT,
            wps_user_id TEXT,
            wps_username TEXT,
            wps_account_id TEXT DEFAULT '',
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS user_llm_keys (
            user_id INTEGER PRIMARY KEY,
            provider TEXT DEFAULT 'deepseek',
            api_key TEXT,
            base_url TEXT,
            model TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            content TEXT NOT NULL,
            type TEXT DEFAULT 'suggestion',
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS reset_tokens (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS email_verify_tokens (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS user_wps_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            file_name TEXT NOT NULL DEFAULT '',
            is_default INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS org_knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            file_name TEXT DEFAULT '',
            is_enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );
        """)
        # 迁移：为旧数据库添加图片模型字段
        for col in ("image_provider", "image_api_key", "image_base_url", "image_model"):
            try:
                conn.execute(f"ALTER TABLE user_llm_keys ADD COLUMN {col} TEXT")
            except Exception:
                pass
        # 迁移：模型能力与高级请求参数（JSON，兼容未来继续扩展）
        for col in ("advanced_config TEXT DEFAULT '{}'", "image_advanced_config TEXT DEFAULT '{}'"):
            try:
                conn.execute(f"ALTER TABLE user_llm_keys ADD COLUMN {col}")
            except Exception:
                pass
        # 迁移：用户活跃表格选择
        try:
            conn.execute("ALTER TABLE users ADD COLUMN active_file_ids TEXT DEFAULT ''")
        except Exception:
            pass
        # 迁移：用户详情字段
        for col in ("real_name TEXT DEFAULT ''", "organization TEXT DEFAULT ''",
                    "job_title TEXT DEFAULT ''", "purpose TEXT DEFAULT ''"):
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col}")
            except Exception:
                pass
        # 迁移：用户记忆表
        conn.execute("""
        CREATE TABLE IF NOT EXISTS user_memory (
            user_id INTEGER PRIMARY KEY,
            memory_text TEXT NOT NULL DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """)
        # 结构化上下文记忆。保留上方 user_memory 作为旧版本兼容与回退，
        # 新数据按用户、话题和 WPS 数据源隔离，避免不同用户/业务互相污染。
        conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            scope_type TEXT NOT NULL DEFAULT 'global',
            scope_id TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT 'general',
            content TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'explicit',
            source_id TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 1.0,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_items_user_scope "
            "ON memory_items(user_id, scope_type, scope_id, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_items_source "
            "ON memory_items(user_id, source_type, source_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_history_user_role "
            "ON chat_history(user_id, role, id DESC)"
        )
        # 迁移：WPS 变更日志表（webhook 推送内容持久化）
        conn.execute("""
        CREATE TABLE IF NOT EXISTS wps_change_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT NOT NULL,
            command TEXT NOT NULL,
            data TEXT DEFAULT '',
            received_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
        """)
        # 迁移：多提供商配置表（每个提供商独立存储配置）
        conn.execute("""
        CREATE TABLE IF NOT EXISTS user_provider_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            api_key TEXT,
            base_url TEXT,
            model TEXT,
            UNIQUE(user_id, provider),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """)
        # 迁移：多提供商图片模型配置表
        conn.execute("""
        CREATE TABLE IF NOT EXISTS user_image_provider_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            api_key TEXT,
            base_url TEXT,
            model TEXT,
            UNIQUE(user_id, provider),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """)
        for table in ("user_provider_configs", "user_image_provider_configs"):
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN advanced_config TEXT DEFAULT '{{}}'")
            except Exception:
                pass
        # 迁移：知识库分类字段
        try:
            conn.execute("ALTER TABLE org_knowledge ADD COLUMN category TEXT DEFAULT '规章制度'")
        except Exception:
            pass
        # 迁移：系统全局配置表（存企业微信等系统级参数）
        conn.execute("""
        CREATE TABLE IF NOT EXISTS system_config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)
        # 迁移：用户企业微信 userid
        try:
            conn.execute("ALTER TABLE users ADD COLUMN wecom_userid TEXT DEFAULT ''")
        except Exception:
            pass
        # 迁移：微信 bot 绑定 id
        try:
            conn.execute("ALTER TABLE users ADD COLUMN weixin_id TEXT DEFAULT ''")
        except Exception:
            pass
        # 迁移：个人微信 ID（用于主动通知）
        try:
            conn.execute("ALTER TABLE users ADD COLUMN personal_weixin_id TEXT DEFAULT ''")
        except Exception:
            pass
        # 迁移：WPS 数字 account_id（用于 Contact 字段写入）
        try:
            conn.execute("ALTER TABLE wps_tokens ADD COLUMN wps_account_id TEXT DEFAULT ''")
        except Exception:
            pass
        # 迁移：多会话对话表
        conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT '新对话',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """)
        # 迁移：chat_history 添加 conversation_id 字段
        try:
            conn.execute("ALTER TABLE chat_history ADD COLUMN conversation_id INTEGER REFERENCES conversations(id)")
        except Exception:
            pass
        # 迁移：知识库向量分块表（RAG）
        conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            knowledge_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            chunk_text TEXT NOT NULL,
            embedding TEXT,
            UNIQUE(knowledge_id, chunk_index),
            FOREIGN KEY(knowledge_id) REFERENCES org_knowledge(id) ON DELETE CASCADE
        )
        """)
        # 迁移：用户提醒表
        conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            content     TEXT NOT NULL,
            remind_at   TEXT NOT NULL,
            event_at    TEXT DEFAULT '',
            retry_count INTEGER DEFAULT 0,
            next_retry_at TEXT DEFAULT '',
            last_error  TEXT DEFAULT '',
            created_at  TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """)
        _create_reminder_delivery_log_table(conn)
        # 迁移：为旧 reminders 表补 event_at 字段
        try:
            conn.execute("ALTER TABLE reminders ADD COLUMN event_at TEXT DEFAULT ''")
        except Exception:
            pass
        for column_sql in (
            "ALTER TABLE reminders ADD COLUMN retry_count INTEGER DEFAULT 0",
            "ALTER TABLE reminders ADD COLUMN next_retry_at TEXT DEFAULT ''",
            "ALTER TABLE reminders ADD COLUMN last_error TEXT DEFAULT ''",
        ):
            try:
                conn.execute(column_sql)
            except Exception:
                pass

        # 驾驶舱快照：按用户、文件、页面和日期保存聚合结果
        conn.execute("""
        CREATE TABLE IF NOT EXISTS dashboard_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            file_id       TEXT NOT NULL,
            view_type     TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            payload       TEXT NOT NULL,
            generated_at  TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(user_id, file_id, view_type, snapshot_date),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """)

        # 驾驶舱本地数据仓库：WPS 仅负责后台同步，页面查询只读 SQLite
        conn.execute("""
        CREATE TABLE IF NOT EXISTS dashboard_data_cache (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            file_id     TEXT NOT NULL,
            data_kind   TEXT NOT NULL,
            payload     TEXT NOT NULL,
            record_count INTEGER DEFAULT 0,
            synced_at   TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(user_id, file_id, data_kind),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """)

        # 远程 MCP 接入令牌：数据库只保存 SHA-256 摘要，明文仅在创建时显示一次
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS mcp_tokens (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            name         TEXT NOT NULL DEFAULT 'WorkBuddy',
            token_hash   TEXT NOT NULL UNIQUE,
            token_prefix TEXT NOT NULL DEFAULT '',
            scopes       TEXT NOT NULL DEFAULT '["all"]',
            is_active    INTEGER NOT NULL DEFAULT 1,
            created_at   TEXT NOT NULL,
            last_used_at TEXT DEFAULT '',
            expires_at   TEXT DEFAULT '',
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS mcp_audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            token_id    INTEGER,
            tool_name   TEXT NOT NULL,
            arguments   TEXT DEFAULT '',
            success     INTEGER NOT NULL DEFAULT 0,
            error       TEXT DEFAULT '',
            duration_ms INTEGER DEFAULT 0,
            created_at  TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(token_id) REFERENCES mcp_tokens(id)
        );

        CREATE INDEX IF NOT EXISTS idx_mcp_tokens_hash ON mcp_tokens(token_hash);
        CREATE INDEX IF NOT EXISTS idx_mcp_audit_user_time ON mcp_audit_log(user_id, created_at);
        """)


# ── 用户 CRUD ──────────────────────────────────────────────

def create_user(username, email, password, is_admin=False, role="staff",
                department="", display_name="", is_enabled=True,
                real_name="", organization="", job_title="", purpose=""):
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO users (username,email,password_hash,is_admin,role,department,display_name,is_enabled,real_name,organization,job_title,purpose) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (username, email, pw_hash, 1 if is_admin else 0,
                 role, department, display_name or username, 1 if is_enabled else 0,
                 real_name, organization, job_title, purpose)
            )
            return True, "注册成功"
        except sqlite3.IntegrityError as e:
            if "username" in str(e):
                return False, "用户名已存在"
            return False, "邮箱已存在"


def get_user_by_email(email):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()


def get_user_by_username(username):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def get_user_by_weixin_id(weixin_id: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE weixin_id=? OR personal_weixin_id=?",
            (weixin_id, weixin_id)
        ).fetchone()


def get_user_by_id(uid):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def verify_password(email, password):
    user = get_user_by_email(email)
    if not user:
        return None
    if not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        return None
    if not user["is_enabled"]:
        return None
    with get_conn() as conn:
        conn.execute("UPDATE users SET last_login=? WHERE id=?",
                     (datetime.now().isoformat(), user["id"]))
    return dict(user)


def list_users():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id,username,email,is_admin,is_enabled,role,department,display_name,plan,created_at,last_login,real_name,organization,job_title,purpose FROM users ORDER BY id"
        ).fetchall()]


def set_user_role(uid, role):
    with get_conn() as conn:
        conn.execute("UPDATE users SET role=? WHERE id=?", (role, uid))


def set_user_department(uid, department):
    with get_conn() as conn:
        conn.execute("UPDATE users SET department=? WHERE id=?", (department, uid))


def set_user_enabled(uid, enabled):
    with get_conn() as conn:
        conn.execute("UPDATE users SET is_enabled=? WHERE id=?", (1 if enabled else 0, uid))


def set_user_admin(uid, is_admin):
    with get_conn() as conn:
        conn.execute("UPDATE users SET is_admin=? WHERE id=?", (1 if is_admin else 0, uid))


def delete_user(uid):
    with get_conn() as conn:
        conn.execute("DELETE FROM wps_tokens WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM user_llm_keys WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM chat_history WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM conversations WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM user_wps_files WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM email_verify_tokens WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))


def reset_password(uid, new_password):
    pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    with get_conn() as conn:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (pw_hash, uid))


def change_password(uid, old_password, new_password):
    user = get_user_by_id(uid)
    if not user or not bcrypt.checkpw(old_password.encode(), user["password_hash"].encode()):
        return False, "原密码错误"
    reset_password(uid, new_password)
    return True, "修改成功"


# ── WPS Token ──────────────────────────────────────────────

def save_wps_token(user_id, access_token, refresh_token, expires_at, wps_user_id="", wps_username=""):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO wps_tokens (user_id, access_token, refresh_token, expires_at, wps_user_id, wps_username)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                access_token=excluded.access_token,
                refresh_token=excluded.refresh_token,
                expires_at=excluded.expires_at,
                wps_user_id=excluded.wps_user_id,
                wps_username=excluded.wps_username
        """, (user_id, access_token, refresh_token, expires_at, wps_user_id, wps_username))


def save_wps_account_id(user_id: int, account_id: str):
    """保存用户的 WPS 数字 account_id（用于 Contact 字段写入）"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE wps_tokens SET wps_account_id=? WHERE user_id=?",
            (account_id, user_id)
        )


def get_wps_account_id_map() -> dict:
    """返回 {wps_user_id: account_id} 映射，用字母格式的 wps_user_id 做 key
    list_contacts 里用 API 返回的字母 id 来查对应的数字 account_id"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT t.wps_user_id, t.wps_account_id
            FROM wps_tokens t
            JOIN users u ON u.id = t.user_id
            WHERE t.wps_account_id != '' AND t.wps_user_id != '' AND u.is_enabled = 1
        """).fetchall()
        result = {}
        for r in rows:
            result[r["wps_user_id"]] = r["wps_account_id"]
        return result


def get_wps_token(user_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM wps_tokens WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def list_users_for_ai() -> list:
    """返回所有启用用户及其 WPS open_id，供 AI 填写 Contact 字段使用"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT u.display_name, u.username, u.role, u.department,
                   COALESCE(t.wps_user_id, '') AS wps_user_id
            FROM users u
            LEFT JOIN wps_tokens t ON t.user_id = u.id AND t.wps_user_id != ''
            WHERE u.is_enabled = 1
            ORDER BY u.id
        """).fetchall()
        result = []
        for r in rows:
            name = r["display_name"] or r["username"]
            result.append({
                "name": name,
                "username": r["username"],
                "role": r["role"],
                "department": r["department"] or "",
                "wps_user_id": r["wps_user_id"],
                "wps_connected": bool(r["wps_user_id"]),
            })
        return result


def get_wps_user_id_by_name(name: str) -> str:
    """按姓名（display_name 或 username）查找对方的 WPS openId，用于@提及"""
    with get_conn() as conn:
        row = conn.execute("""
            SELECT t.wps_user_id FROM wps_tokens t
            JOIN users u ON u.id = t.user_id
            WHERE (u.display_name=? OR u.username=?) AND t.wps_user_id != ''
        """, (name, name)).fetchone()
        return row["wps_user_id"] if row else ""


# ── LLM Key（多提供商配置记忆）────────────────────────────

def _decode_advanced_config(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def save_llm_key(user_id, provider, api_key, base_url, model, advanced_config=None):
    """保存主模型配置（同时保存到当前配置和提供商配置表）"""
    with get_conn() as conn:
        # 更新当前使用的配置
        conn.execute("""
            INSERT INTO user_llm_keys (user_id, provider, api_key, base_url, model, advanced_config)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                provider=excluded.provider,
                api_key=excluded.api_key,
                base_url=excluded.base_url,
                model=excluded.model,
                advanced_config=COALESCE(excluded.advanced_config, user_llm_keys.advanced_config)
        """, (user_id, provider, api_key, base_url, model,
              json.dumps(advanced_config, ensure_ascii=False) if advanced_config is not None else None))

        # 同时保存到提供商配置表（记忆功能）
        conn.execute("""
            INSERT INTO user_provider_configs (user_id, provider, api_key, base_url, model, advanced_config)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(user_id, provider) DO UPDATE SET
                api_key=excluded.api_key,
                base_url=excluded.base_url,
                model=excluded.model,
                advanced_config=COALESCE(excluded.advanced_config, user_provider_configs.advanced_config)
        """, (user_id, provider, api_key, base_url, model,
              json.dumps(advanced_config, ensure_ascii=False) if advanced_config is not None else None))


def get_llm_key(user_id):
    """获取当前使用的主模型配置"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM user_llm_keys WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["advanced"] = _decode_advanced_config(result.get("advanced_config"))
        return result


def get_provider_config(user_id, provider):
    """获取指定提供商的已保存配置"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT api_key, base_url, model, advanced_config FROM user_provider_configs WHERE user_id=? AND provider=?",
            (user_id, provider)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["advanced"] = _decode_advanced_config(result.get("advanced_config"))
        return result


def list_custom_provider_configs(user_id, image: bool = False):
    """列出用户保存的自定义 OpenAI 兼容模型档案（不返回密钥）。"""
    table = "user_image_provider_configs" if image else "user_provider_configs"
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT provider, base_url, model, advanced_config
                FROM {table}
                WHERE user_id=? AND provider LIKE 'custom_openai:%'
                ORDER BY id DESC""",
            (user_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["advanced"] = _decode_advanced_config(item.pop("advanced_config", "{}"))
            result.append(item)
        return result


def delete_custom_provider_config(user_id, provider, image: bool = False):
    """删除指定自定义模型档案；调用方负责阻止删除当前正在使用的档案。"""
    table = "user_image_provider_configs" if image else "user_provider_configs"
    with get_conn() as conn:
        conn.execute(
            f"DELETE FROM {table} WHERE user_id=? AND provider=? AND provider LIKE 'custom_openai:%'",
            (user_id, provider),
        )


def save_image_llm_key(user_id, provider, api_key, base_url, model, advanced_config=None):
    """保存图片模型配置（同时保存到当前配置和提供商配置表）"""
    with get_conn() as conn:
        # 确保 user_llm_keys 记录存在
        conn.execute(
            "INSERT OR IGNORE INTO user_llm_keys (user_id) VALUES (?)", (user_id,)
        )
        # 更新当前使用的图片模型配置
        conn.execute(
            """UPDATE user_llm_keys
               SET image_provider=?, image_api_key=?, image_base_url=?, image_model=?,
                   image_advanced_config=COALESCE(?, image_advanced_config)
               WHERE user_id=?""",
            (provider, api_key, base_url, model,
             json.dumps(advanced_config, ensure_ascii=False) if advanced_config is not None else None,
             user_id)
        )

        # 同时保存到图片提供商配置表（记忆功能）
        conn.execute("""
            INSERT INTO user_image_provider_configs (user_id, provider, api_key, base_url, model, advanced_config)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(user_id, provider) DO UPDATE SET
                api_key=excluded.api_key,
                base_url=excluded.base_url,
                model=excluded.model,
                advanced_config=COALESCE(excluded.advanced_config, user_image_provider_configs.advanced_config)
        """, (user_id, provider, api_key, base_url, model,
              json.dumps(advanced_config, ensure_ascii=False) if advanced_config is not None else None))


def get_image_llm_key(user_id):
    """获取当前使用的图片模型配置"""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT image_provider, image_api_key, image_base_url, image_model,
                      image_advanced_config
               FROM user_llm_keys WHERE user_id=?""",
            (user_id,)
        ).fetchone()
        if not row or not row["image_api_key"]:
            return None
        return {
            "provider": row["image_provider"] or "qwen",
            "api_key": row["image_api_key"],
            "base_url": row["image_base_url"],
            "model": row["image_model"],
            "advanced": _decode_advanced_config(row["image_advanced_config"]),
        }


def get_image_provider_config(user_id, provider):
    """获取指定提供商的已保存图片模型配置"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT api_key, base_url, model, advanced_config FROM user_image_provider_configs WHERE user_id=? AND provider=?",
            (user_id, provider)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["advanced"] = _decode_advanced_config(result.get("advanced_config"))
        return result


# ── 反馈 ───────────────────────────────────────────────────

def add_feedback(user_id, content, fb_type="suggestion"):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO feedback (user_id, content, type) VALUES (?,?,?)",
            (user_id, content, fb_type)
        )


def list_feedback(status=None):
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT f.*,u.username FROM feedback f LEFT JOIN users u ON f.user_id=u.id WHERE f.status=? ORDER BY f.id DESC",
                (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT f.*,u.username FROM feedback f LEFT JOIN users u ON f.user_id=u.id ORDER BY f.id DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def update_feedback_status(fid, status):
    with get_conn() as conn:
        conn.execute("UPDATE feedback SET status=? WHERE id=?", (status, fid))


# ── 对话历史 ───────────────────────────────────────────────

def create_conversation(user_id: int, title: str = "新对话") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (user_id, title) VALUES (?,?)",
            (user_id, title)
        )
        return cur.lastrowid


def list_conversations(user_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM conversations WHERE user_id=? ORDER BY updated_at DESC",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_last_active_conv_id(user_id: int) -> int | None:
    """返回该用户最近有消息记录的对话 ID，避免空的新对话被选为默认"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT conversation_id FROM chat_history "
            "WHERE user_id=? AND conversation_id IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (user_id,)
        ).fetchone()
        return int(row["conversation_id"]) if row else None


def get_conversation(conv_id: int, user_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, title, created_at, updated_at FROM conversations WHERE id=? AND user_id=?",
            (conv_id, user_id)
        ).fetchone()
        return dict(row) if row else None


def rename_conversation(conv_id: int, user_id: int, title: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE conversations SET title=? WHERE id=? AND user_id=?",
            (title, conv_id, user_id)
        )


def delete_conversation(conv_id: int, user_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM chat_history WHERE conversation_id=?", (conv_id,))
        conn.execute(
            "DELETE FROM memory_items WHERE user_id=? AND scope_type='conversation' AND scope_id=?",
            (user_id, str(conv_id)),
        )
        conn.execute("DELETE FROM conversations WHERE id=? AND user_id=?", (conv_id, user_id))


def touch_conversation(conv_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE conversations SET updated_at=datetime('now') WHERE id=?",
            (conv_id,)
        )


def add_chat(user_id: int, role: str, content: str, conv_id: int = None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO chat_history (user_id, role, content, conversation_id) VALUES (?,?,?,?)",
            (user_id, role, content, conv_id)
        )
    if conv_id:
        touch_conversation(conv_id)


def get_chat_history(user_id: int, conv_id: int = None, limit: int = 20) -> list:
    with get_conn() as conn:
        if conv_id is not None:
            rows = conn.execute(
                "SELECT role, content, created_at FROM chat_history "
                "WHERE user_id=? AND conversation_id=? ORDER BY id DESC LIMIT ?",
                (user_id, conv_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT role, content, created_at FROM chat_history WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit)
            ).fetchall()
        return list(reversed([dict(r) for r in rows]))


def clear_chat_history(user_id: int, conv_id: int = None):
    with get_conn() as conn:
        if conv_id is not None:
            conn.execute("DELETE FROM chat_history WHERE user_id=? AND conversation_id=?", (user_id, conv_id))
            conn.execute(
                "DELETE FROM memory_items WHERE user_id=? AND scope_type='conversation' AND scope_id=?",
                (user_id, str(conv_id)),
            )
        else:
            conn.execute("DELETE FROM chat_history WHERE user_id=?", (user_id,))
            conn.execute(
                "DELETE FROM memory_items WHERE user_id=? AND scope_type='conversation'",
                (user_id,),
            )


def get_chat_count(user_id: int, conv_id: int = None) -> int:
    with get_conn() as conn:
        if conv_id is not None:
            row = conn.execute(
                "SELECT COUNT(*) FROM chat_history WHERE user_id=? AND conversation_id=?",
                (user_id, conv_id)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM chat_history WHERE user_id=?", (user_id,)
            ).fetchone()
        return row[0] if row else 0


# ── 密码重置 Token ──────────────────────────────────────────

def create_reset_token(email: str) -> tuple[bool, str]:
    """为邮箱创建重置token，返回 (ok, token_or_errmsg)"""
    user = get_user_by_email(email)
    if not user:
        return False, "该邮箱未注册"
    token = secrets.token_urlsafe(32)
    from datetime import timedelta
    expires_at = (datetime.now() + timedelta(hours=24)).isoformat()
    with get_conn() as conn:
        # 清除旧token
        conn.execute("DELETE FROM reset_tokens WHERE user_id=?", (user["id"],))
        conn.execute(
            "INSERT INTO reset_tokens (token, user_id, expires_at) VALUES (?,?,?)",
            (token, user["id"], expires_at)
        )
    return True, token


def verify_reset_token(token: str):
    """验证token有效性，返回 user_id 或 None"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM reset_tokens WHERE token=? AND used=0", (token,)
        ).fetchone()
    if not row:
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now():
        return None
    return row["user_id"]


def consume_reset_token(token: str, new_password: str) -> tuple[bool, str]:
    """使用token重置密码"""
    user_id = verify_reset_token(token)
    if not user_id:
        return False, "链接无效或已过期"
    reset_password(user_id, new_password)
    with get_conn() as conn:
        conn.execute("UPDATE reset_tokens SET used=1 WHERE token=?", (token,))
    return True, "密码重置成功"


# ── WPS 文件管理 ────────────────────────────────────────────

def add_wps_file(user_id: int, file_id: str, file_name: str) -> tuple[bool, str]:
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM user_wps_files WHERE user_id=? AND file_id=?",
            (user_id, file_id)
        ).fetchone()
        if existing:
            conn.execute("UPDATE user_wps_files SET file_name=? WHERE user_id=? AND file_id=?",
                         (file_name, user_id, file_id))
            return True, "已更新"
        conn.execute(
            "INSERT INTO user_wps_files (user_id, file_id, file_name) VALUES (?,?,?)",
            (user_id, file_id, file_name)
        )
        # 如果是第一个，自动设为默认
        count = conn.execute("SELECT COUNT(*) FROM user_wps_files WHERE user_id=?", (user_id,)).fetchone()[0]
        if count == 1:
            conn.execute("UPDATE user_wps_files SET is_default=1 WHERE user_id=? AND file_id=?",
                         (user_id, file_id))
    return True, "添加成功"


def list_wps_files(user_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM user_wps_files WHERE user_id=? ORDER BY is_default DESC, id ASC",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def set_default_wps_file(user_id: int, file_id: str):
    with get_conn() as conn:
        conn.execute("UPDATE user_wps_files SET is_default=0 WHERE user_id=?", (user_id,))
        conn.execute("UPDATE user_wps_files SET is_default=1 WHERE user_id=? AND file_id=?",
                     (user_id, file_id))


def delete_wps_file(user_id: int, file_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM user_wps_files WHERE user_id=? AND file_id=?", (user_id, file_id))
        # 如果删的是默认，把第一个设为默认
        first = conn.execute(
            "SELECT file_id FROM user_wps_files WHERE user_id=? ORDER BY id ASC LIMIT 1",
            (user_id,)
        ).fetchone()
        if first:
            conn.execute("UPDATE user_wps_files SET is_default=1 WHERE user_id=? AND file_id=?",
                         (user_id, first["file_id"]))


# ── 规章制度知识库 ──────────────────────────────────────────

def add_knowledge(title: str, content: str, file_name: str = "", category: str = "规章制度") -> int:
    """添加知识条目，返回新记录 id"""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO org_knowledge (title, content, file_name, category) VALUES (?,?,?,?)",
            (title, content, file_name, category or "规章制度")
        )
        return cur.lastrowid


def upsert_knowledge(title: str, content: str, file_name: str, category: str = "规章制度") -> tuple[int, bool]:
    """按 file_name 去重：已存在则覆盖，否则新建。返回 (id, is_update)"""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM org_knowledge WHERE file_name=?", (file_name,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE org_knowledge SET title=?, content=?, category=? WHERE id=?",
                (title, content, category or "规章制度", existing["id"])
            )
            return existing["id"], True
        cur = conn.execute(
            "INSERT INTO org_knowledge (title, content, file_name, category) VALUES (?,?,?,?)",
            (title, content, file_name, category or "规章制度")
        )
        return cur.lastrowid, False


def bulk_delete_knowledge(ids: list) -> int:
    """批量删除，返回实际删除数量"""
    if not ids:
        return 0
    with get_conn() as conn:
        conn.executemany("DELETE FROM org_knowledge WHERE id=?", [(i,) for i in ids])
    return len(ids)


def list_knowledge(enabled_only: bool = False) -> list:
    with get_conn() as conn:
        if enabled_only:
            rows = conn.execute(
                "SELECT * FROM org_knowledge WHERE is_enabled=1 ORDER BY id DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM org_knowledge ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def toggle_knowledge(kid: int, enabled: bool):
    with get_conn() as conn:
        conn.execute("UPDATE org_knowledge SET is_enabled=? WHERE id=?",
                     (1 if enabled else 0, kid))


def delete_knowledge(kid: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM org_knowledge WHERE id=?", (kid,))


def search_knowledge(query: str, limit: int = 5) -> list:
    """简单关键词搜索，返回最多 limit 条匹配的知识条目（仅已启用）"""
    if not query or not query.strip():
        return []
    # 多关键词 AND 搜索：��个词都要出现在 title 或 content 中
    keywords = [w.strip() for w in query.split() if w.strip()][:5]
    with get_conn() as conn:
        # 先尝试全词命中
        conditions = " AND ".join(
            "(title LIKE ? OR content LIKE ?)" for _ in keywords
        )
        params = []
        for kw in keywords:
            params.extend([f"%{kw}%", f"%{kw}%"])
        params.append(limit)
        rows = conn.execute(
            f"SELECT id, title, content, file_name FROM org_knowledge "
            f"WHERE is_enabled=1 AND {conditions} ORDER BY id DESC LIMIT ?",
            params
        ).fetchall()
        if rows:
            return [dict(r) for r in rows]
        # 回退：只要任一关键词命中
        conditions2 = " OR ".join(
            "(title LIKE ? OR content LIKE ?)" for _ in keywords
        )
        rows2 = conn.execute(
            f"SELECT id, title, content, file_name FROM org_knowledge "
            f"WHERE is_enabled=1 AND ({conditions2}) ORDER BY id DESC LIMIT ?",
            params[:-1] + [limit]
        ).fetchall()
        return [dict(r) for r in rows2]


# ── 知识库向量嵌入（RAG）─────────────────────────────────────

def get_embed_config() -> dict | None:
    """返回向量嵌入配置，未配置时返回 None"""
    key = get_system_config("embed_api_key")
    if not key:
        return None
    return {
        "api_key": key,
        "base_url": get_system_config("embed_base_url", "https://api.siliconflow.cn/v1"),
        "model": get_system_config("embed_model", "BAAI/bge-m3"),
    }


def save_embed_config(api_key: str, base_url: str, model: str):
    set_system_config("embed_api_key", api_key)
    set_system_config("embed_base_url", base_url or "https://api.siliconflow.cn/v1")
    set_system_config("embed_model", model or "BAAI/bge-m3")


def save_knowledge_chunks(knowledge_id: int, chunks: list[tuple[int, str, str]]):
    """存储分块向量，chunks 每项为 (chunk_index, chunk_text, embedding_json)"""
    with get_conn() as conn:
        conn.execute("DELETE FROM knowledge_chunks WHERE knowledge_id=?", (knowledge_id,))
        conn.executemany(
            "INSERT INTO knowledge_chunks (knowledge_id, chunk_index, chunk_text, embedding) VALUES (?,?,?,?)",
            [(knowledge_id, idx, text, emb) for idx, text, emb in chunks]
        )


def get_chunk_counts() -> dict[int, int]:
    """返回每个知识文档的已嵌入块数，{knowledge_id: count}"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT knowledge_id, COUNT(*) as cnt FROM knowledge_chunks GROUP BY knowledge_id"
        ).fetchall()
    return {r["knowledge_id"]: r["cnt"] for r in rows}


def search_knowledge_rag(query_vector: list[float], limit: int = 5) -> list[dict]:
    """
    向量检索：返回最相似的 limit 条知识文档，每条附带最佳匹配块文本和相似度分数。
    只检索已启用 (is_enabled=1) 的文档。
    """
    import math, json as _json

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT kc.knowledge_id, kc.chunk_text, kc.embedding,
                   ok.title, ok.content, ok.file_name
            FROM knowledge_chunks kc
            JOIN org_knowledge ok ON ok.id = kc.knowledge_id
            WHERE ok.is_enabled = 1 AND kc.embedding IS NOT NULL
            """
        ).fetchall()

    if not rows:
        return []

    def cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na and nb else 0.0

    best: dict[int, dict] = {}
    for row in rows:
        try:
            vec = _json.loads(row["embedding"])
        except Exception:
            continue
        score = cosine(query_vector, vec)
        kid = row["knowledge_id"]
        if kid not in best or score > best[kid]["score"]:
            best[kid] = {
                "id": kid,
                "title": row["title"],
                "content": row["content"],
                "file_name": row["file_name"],
                "chunk_text": row["chunk_text"],
                "score": score,
            }

    ranked = sorted(best.values(), key=lambda x: x["score"], reverse=True)
    return ranked[:limit]


def create_email_verify_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    from datetime import timedelta
    expires_at = (datetime.now() + timedelta(hours=24)).isoformat()
    with get_conn() as conn:
        conn.execute("DELETE FROM email_verify_tokens WHERE user_id=?", (user_id,))
        conn.execute(
            "INSERT INTO email_verify_tokens (token, user_id, expires_at) VALUES (?,?,?)",
            (token, user_id, expires_at)
        )
    return token


def consume_email_verify_token(token: str) -> tuple[bool, str]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM email_verify_tokens WHERE token=? AND used=0", (token,)
        ).fetchone()
        if not row:
            return False, "验证链接无效或已使用"
        if datetime.fromisoformat(row["expires_at"]) < datetime.now():
            return False, "验证链接已过期，请重新注册"
        conn.execute("UPDATE users SET is_enabled=1 WHERE id=?", (row["user_id"],))
        conn.execute("UPDATE email_verify_tokens SET used=1 WHERE token=?", (token,))
    return True, "邮箱验证成功"


def get_default_wps_file(user_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM user_wps_files WHERE user_id=? AND is_default=1",
            (user_id,)
        ).fetchone()
        return dict(row) if row else None

# ── 用户记忆 ───────────────────────────────────────────────

def get_user_memory(user_id: int) -> str:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT memory_text FROM user_memory WHERE user_id=?", (user_id,)
        ).fetchone()
        return row["memory_text"] if row else ""


def save_user_memory(user_id: int, memory_text: str):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO user_memory (user_id, memory_text, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
                memory_text=excluded.memory_text,
                updated_at=excluded.updated_at
        """, (user_id, memory_text))


def save_memory_item(user_id: int, content: str, *, scope_type: str = "global",
                     scope_id: str = "", category: str = "general",
                     source_type: str = "explicit", source_id: str = "",
                     confidence: float = 1.0, replace_source: bool = False) -> int:
    """保存一条有来源、可隔离的上下文记忆，返回记录 ID。

    ``replace_source`` 用于话题摘要等单一派生结果；显式记忆和自动提取默认按
    完整内容去重，以便保留证据而不重复堆叠。
    """
    allowed_scopes = {"global", "conversation", "file", "contact", "workspace"}
    scope_type = scope_type if scope_type in allowed_scopes else "global"
    scope_id = str(scope_id or "")
    content = str(content or "").strip()
    if not content:
        return 0
    try:
        confidence = max(0.0, min(float(confidence), 1.0))
    except (TypeError, ValueError):
        confidence = 1.0

    with get_conn() as conn:
        row = None
        if replace_source and source_type and source_id:
            row = conn.execute(
                "SELECT id FROM memory_items WHERE user_id=? AND scope_type=? AND scope_id=? "
                "AND source_type=? AND source_id=? LIMIT 1",
                (user_id, scope_type, scope_id, source_type, str(source_id)),
            ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT id FROM memory_items WHERE user_id=? AND scope_type=? AND scope_id=? "
                "AND category=? AND content=? AND status='active' LIMIT 1",
                (user_id, scope_type, scope_id, category, content),
            ).fetchone()
        if row:
            conn.execute(
                "UPDATE memory_items SET content=?, category=?, source_type=?, source_id=?, "
                "confidence=?, status='active', updated_at=datetime('now') WHERE id=?",
                (content, category, source_type, str(source_id or ""), confidence, row["id"]),
            )
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO memory_items "
            "(user_id, scope_type, scope_id, category, content, source_type, source_id, confidence) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (user_id, scope_type, scope_id, category, content,
             source_type, str(source_id or ""), confidence),
        )
        return int(cur.lastrowid)


def list_memory_items(user_id: int, *, scope_type: str | None = None,
                      scope_ids: list[str] | None = None, limit: int = 100) -> list:
    """列出当前用户可用记忆；调用方必须明确传入用户，天然隔离租户。"""
    clauses = ["user_id=?", "status='active'"]
    params: list = [user_id]
    if scope_type:
        clauses.append("scope_type=?")
        params.append(scope_type)
    if scope_ids is not None:
        normalized = [str(value) for value in scope_ids if str(value or "")]
        if not normalized:
            return []
        placeholders = ",".join("?" for _ in normalized)
        clauses.append(f"scope_id IN ({placeholders})")
        params.extend(normalized)
    params.append(max(1, min(int(limit), 500)))
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM memory_items WHERE " + " AND ".join(clauses)
            + " ORDER BY confidence DESC, updated_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def get_memory_item_by_source(user_id: int, source_type: str, source_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM memory_items WHERE user_id=? AND source_type=? AND source_id=? "
            "AND status='active' ORDER BY id DESC LIMIT 1",
            (user_id, source_type, str(source_id)),
        ).fetchone()
        return dict(row) if row else None


def get_user_chat_candidates(user_id: int, limit: int = 500) -> list:
    """返回用于历史召回的用户原话；不返回 AI 回复，防止模型自我强化。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT h.id, h.conversation_id, h.content, h.created_at, "
            "COALESCE(c.title, '') AS conversation_title "
            "FROM chat_history h LEFT JOIN conversations c ON c.id=h.conversation_id "
            "WHERE h.user_id=? AND h.role='user' ORDER BY h.id DESC LIMIT ?",
            (user_id, max(1, min(int(limit), 2000))),
        ).fetchall()
        return [dict(row) for row in rows]


# ── 用户活跃表格选择 ─────────────────────────────────────────

def get_active_file_ids(user_id: int) -> list:
    with get_conn() as conn:
        row = conn.execute("SELECT active_file_ids FROM users WHERE id=?", (user_id,)).fetchone()
        val = row["active_file_ids"] if row else ""
        return [x for x in val.split(",") if x] if val else []


def set_active_file_ids(user_id: int, ids: list):
    with get_conn() as conn:
        conn.execute("UPDATE users SET active_file_ids=? WHERE id=?",
                     (",".join(ids), user_id))


# ── WPS 变更日志 ────────────────────────────────────────────

def add_change_log(file_id: str, command: str, data: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO wps_change_log (file_id, command, data) VALUES (?,?,?)",
            (file_id, command, data)
        )


def get_change_log(file_id: str = None, limit: int = 50, since_id: int = 0) -> list:
    with get_conn() as conn:
        if file_id:
            rows = conn.execute(
                "SELECT * FROM wps_change_log WHERE file_id=? AND id>? ORDER BY id DESC LIMIT ?",
                (file_id, since_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM wps_change_log WHERE id>? ORDER BY id DESC LIMIT ?",
                (since_id, limit)
            ).fetchall()
        return [dict(r) for r in rows]


def get_change_log_last_seen(user_id: int) -> int:
    """返回用户上次查看变更日志时的最大 log id，0 表示从未查过"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM system_config WHERE key=?",
            (f"change_log_last_seen_{user_id}",)
        ).fetchone()
    return int(row["value"]) if row else 0


def set_change_log_last_seen(user_id: int, log_id: int):
    """记录用户本次查看变更日志时的最大 log id"""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO system_config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"change_log_last_seen_{user_id}", str(log_id))
        )


# ── 系统全局配置 ────────────────────────────────────────────

def get_system_config(key: str, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM system_config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_system_config(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO system_config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )


def get_wecom_config() -> dict | None:
    """返回企业微信配置，未配置时返回 None"""
    corpid = get_system_config("wecom_corpid")
    agentid = get_system_config("wecom_agentid")
    secret = get_system_config("wecom_secret")
    if not (corpid and agentid and secret):
        return None
    return {"corpid": corpid, "agentid": int(agentid), "secret": secret}


# ── 用户企业微信 userid ──────────────────────────────────────

def get_wecom_userid(username: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT wecom_userid FROM users WHERE username=? OR display_name=? LIMIT 1",
            (username, username)
        ).fetchone()
    return row["wecom_userid"] if row and row["wecom_userid"] else None


def set_wecom_userid(user_id: int, wecom_userid: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET wecom_userid=? WHERE id=?", (wecom_userid, user_id)
        )


def set_weixin_id(user_id: int, weixin_id: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET weixin_id=? WHERE id=?", (weixin_id, user_id)
        )


def get_personal_weixin_id(user_id: int) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT personal_weixin_id FROM users WHERE id=?", (user_id,)).fetchone()
        return row["personal_weixin_id"] if row else ""


def set_personal_weixin_id(user_id: int, personal_weixin_id: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET personal_weixin_id=? WHERE id=?", (personal_weixin_id, user_id)
        )


def set_display_name(user_id: int, display_name: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET display_name=? WHERE id=?", (display_name, user_id)
        )


def get_user_by_display_name(name: str):
    """按 display_name 或 username 查找用户"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE display_name=? OR username=? LIMIT 1",
            (name, name)
        ).fetchone()
        return dict(row) if row else None


def get_any_valid_wps_token() -> dict | None:
    """返回任意一个未过期的 WPS token（用于 webhook 回调时调用 API）"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM wps_tokens WHERE expires_at > datetime('now') ORDER BY user_id LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def list_all_wps_files() -> list:
    """返回所有用户配置的 WPS 文件列表"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT file_id, file_name FROM user_wps_files"
        ).fetchall()
        return [dict(r) for r in rows]


def get_hook_file_id(webhook_id: str) -> str:
    """根据 webhook_id 查找对应的 file_id（暂时返回空，后续可扩展）"""
    return ""


# ── 提醒 CRUD ──────────────────────────────────────────────

def init_reminders_table():
    """确保 reminders 表存在（在 init_db 之后调用，或独立调用均可）"""
    with get_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            content     TEXT NOT NULL,
            remind_at   TEXT NOT NULL,
            event_at    TEXT DEFAULT '',
            retry_count INTEGER DEFAULT 0,
            next_retry_at TEXT DEFAULT '',
            last_error  TEXT DEFAULT '',
            created_at  TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """)
        _create_reminder_delivery_log_table(conn)


def log_reminder_delivery(
    reminder_id: int,
    user_id: int,
    remind_at: str,
    event_at: str,
    channel: str,
    target: str,
    status: str,
    detail: str = "",
) -> int:
    """记录每次最终投递结果，成功提醒删除后仍可追溯。"""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO reminder_delivery_log "
            "(reminder_id, user_id, remind_at, event_at, channel, target, status, detail, attempted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                reminder_id, user_id, remind_at or "", event_at or "",
                channel or "", target or "", status, (detail or "")[:1000],
                beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        return cur.lastrowid


def list_reminder_delivery_log(user_id: int, limit: int = 100) -> list:
    """返回用户最近的提醒投递审计记录。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM reminder_delivery_log WHERE user_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, max(1, min(int(limit), 500))),
        ).fetchall()
        return [dict(row) for row in rows]


def add_reminder(
    user_id: int,
    content: str,
    remind_at: str,
    event_at: str = "",
) -> int:
    """
    新增一条精确定时提醒。

    remind_at 表示用户要求收到消息的触发时间，event_at 表示事情发生时间。
    两者都不再被擅自改写。格式必须为 'YYYY-MM-DD HH:MM'（本地时间）。
    """
    remind_at_normalized = datetime.strptime(
        remind_at, "%Y-%m-%d %H:%M"
    ).strftime("%Y-%m-%d %H:%M")
    event_at_normalized = datetime.strptime(
        event_at or remind_at, "%Y-%m-%d %H:%M"
    ).strftime("%Y-%m-%d %H:%M")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (user_id, content, remind_at, event_at, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                user_id, content, remind_at_normalized, event_at_normalized,
                beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        return cur.lastrowid


def list_reminders(user_id: int) -> list:
    """返回该用户所有待触发提醒，按触发时间升序。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, content, remind_at, retry_count, next_retry_at, last_error, created_at "
            "FROM reminders "
            "WHERE user_id = ? ORDER BY remind_at ASC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def cancel_reminder(reminder_id: int, user_id: int) -> bool:
    """取消（删除）指定提醒，仅限本人操作，返回是否删除成功。"""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM reminders WHERE id = ? AND user_id = ?",
            (reminder_id, user_id),
        )
        return cur.rowcount > 0


def get_due_reminders(now: datetime | None = None) -> list:
    """
    返回所有已到期的提醒（remind_at <= 当前本地时间）。
    调用方在推送后负责调用 delete_reminder 删除记录。
    """
    now_text = (now or beijing_now()).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT r.id, r.user_id, r.content, r.remind_at, r.event_at, "
            "       r.retry_count, r.next_retry_at, r.last_error, "
            "       u.weixin_id, u.personal_weixin_id, u.display_name "
            "FROM reminders r JOIN users u ON u.id = r.user_id "
            "WHERE r.remind_at <= ? "
            "  AND (r.next_retry_at IS NULL OR r.next_retry_at = '' "
            "       OR r.next_retry_at <= ?)",
            (now_text, now_text),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_reminder(reminder_id: int) -> None:
    """触发后删除提醒记录，避免积压。"""
    with get_conn() as conn:
        conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))


def mark_reminder_failed(
    reminder_id: int,
    error: str,
    now: datetime | None = None,
) -> None:
    """记录推送失败并安排退避重试；提醒不会因通道临时故障而丢失。"""
    retry_delays = (1, 5, 15, 30, 60)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT retry_count FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()
        if not row:
            return
        retry_count = int(row["retry_count"] or 0) + 1
        delay_minutes = retry_delays[min(retry_count - 1, len(retry_delays) - 1)]
        next_retry_at = (now or beijing_now()) + timedelta(minutes=delay_minutes)
        conn.execute(
            "UPDATE reminders "
            "SET retry_count = ?, "
            "    next_retry_at = ?, "
            "    last_error = ? "
            "WHERE id = ?",
            (
                retry_count,
                next_retry_at.strftime("%Y-%m-%d %H:%M:%S"),
                (error or "")[:1000],
                reminder_id,
            ),
        )


def cleanup_legacy_reminders() -> int:
    """
    删除旧格式记录（event_at 为空）。

    已过触发时间但尚未确认送达的新格式提醒必须保留，由调度器继续重试。
    返回删除的行数。
    """
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM reminders "
            "WHERE event_at IS NULL OR event_at = ''"
        )
        return cur.rowcount


# ── 驾驶舱快照 ─────────────────────────────────────────────

def save_dashboard_snapshot(
    user_id: int,
    file_id: str,
    view_type: str,
    snapshot_date: str,
    payload: dict,
) -> None:
    """新增或覆盖同一用户/文件/页面/日期的驾驶舱快照。"""
    import json

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO dashboard_snapshots
                (user_id, file_id, view_type, snapshot_date, payload, generated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))
            ON CONFLICT(user_id, file_id, view_type, snapshot_date)
            DO UPDATE SET payload=excluded.payload,
                          generated_at=datetime('now', 'localtime')
            """,
            (
                user_id,
                file_id,
                view_type,
                snapshot_date,
                json.dumps(payload, ensure_ascii=False),
            ),
        )


def get_dashboard_snapshot(
    user_id: int,
    file_id: str,
    view_type: str,
    snapshot_date: str,
) -> dict | None:
    """读取驾驶舱快照，返回 payload 和 generated_at。"""
    import json

    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT payload, generated_at
            FROM dashboard_snapshots
            WHERE user_id=? AND file_id=? AND view_type=? AND snapshot_date=?
            """,
            (user_id, file_id, view_type, snapshot_date),
        ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload"])
        except Exception:
            return None
        payload["generated_at"] = row["generated_at"]
        return payload


def list_dashboard_snapshot_dates(
    user_id: int,
    file_id: str,
    view_type: str = "daily",
    limit: int = 366,
) -> list[str]:
    """返回已有快照日期，最新日期在前。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT snapshot_date
            FROM dashboard_snapshots
            WHERE user_id=? AND file_id=? AND view_type=?
            ORDER BY snapshot_date DESC
            LIMIT ?
            """,
            (user_id, file_id, view_type, max(1, min(int(limit), 2000))),
        ).fetchall()
        return [row["snapshot_date"] for row in rows]


def save_dashboard_data_cache(
    user_id: int,
    file_id: str,
    data_kind: str,
    payload: dict,
) -> None:
    """覆盖保存一类 WPS 原始数据的本地缓存。"""
    import json

    records = payload.get("records", []) if isinstance(payload, dict) else []
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO dashboard_data_cache
                (user_id, file_id, data_kind, payload, record_count, synced_at)
            VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))
            ON CONFLICT(user_id, file_id, data_kind)
            DO UPDATE SET payload=excluded.payload,
                          record_count=excluded.record_count,
                          synced_at=datetime('now', 'localtime')
            """,
            (user_id, file_id, data_kind, json.dumps(payload, ensure_ascii=False), len(records)),
        )


def get_dashboard_data_cache(user_id: int, file_id: str, data_kind: str) -> dict | None:
    """读取一类本地数据及同步时间。"""
    import json

    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT payload, record_count, synced_at
            FROM dashboard_data_cache
            WHERE user_id=? AND file_id=? AND data_kind=?
            """,
            (user_id, file_id, data_kind),
        ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload"])
        except Exception:
            return None
        payload["record_count"] = row["record_count"]
        payload["synced_at"] = row["synced_at"]
        return payload


def get_dashboard_cache_status(user_id: int, file_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT data_kind, record_count, synced_at
            FROM dashboard_data_cache
            WHERE user_id=? AND file_id=?
            ORDER BY data_kind
            """,
            (user_id, file_id),
        ).fetchall()
        return [dict(row) for row in rows]


# ── MCP 访问令牌与审计 ─────────────────────────────────────

def create_mcp_token(user_id: int, name: str = "WorkBuddy", expires_days: int | None = None) -> dict:
    """创建 MCP Bearer token；明文只由本函数返回一次。"""
    raw_token = "onx_mcp_" + secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    now = beijing_now()
    expires_at = (now + timedelta(days=expires_days)).strftime("%Y-%m-%d %H:%M:%S") if expires_days else ""
    safe_name = (name or "WorkBuddy").strip()[:80]
    with get_conn() as conn:
        cursor = conn.execute(
            """INSERT INTO mcp_tokens
               (user_id, name, token_hash, token_prefix, scopes, is_active, created_at, expires_at)
               VALUES (?, ?, ?, ?, '[\"all\"]', 1, ?, ?)""",
            (user_id, safe_name, token_hash, raw_token[:16], now.strftime("%Y-%m-%d %H:%M:%S"), expires_at),
        )
        token_id = cursor.lastrowid
    return {
        "id": token_id,
        "name": safe_name,
        "token": raw_token,
        "token_prefix": raw_token[:16],
        "expires_at": expires_at,
    }


def verify_mcp_token(raw_token: str) -> dict | None:
    if not raw_token or not raw_token.startswith("onx_mcp_"):
        return None
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    now = beijing_now()
    with get_conn() as conn:
        row = conn.execute(
            """SELECT t.*, u.username, u.display_name, u.is_admin, u.is_enabled
               FROM mcp_tokens t JOIN users u ON u.id=t.user_id
               WHERE t.token_hash=? AND t.is_active=1""",
            (token_hash,),
        ).fetchone()
        if not row or not row["is_enabled"]:
            return None
        if row["expires_at"]:
            try:
                if now >= datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S"):
                    return None
            except ValueError:
                return None
        conn.execute(
            "UPDATE mcp_tokens SET last_used_at=? WHERE id=?",
            (now.strftime("%Y-%m-%d %H:%M:%S"), row["id"]),
        )
        return dict(row)


def list_mcp_tokens(user_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, name, token_prefix, scopes, is_active, created_at, last_used_at, expires_at
               FROM mcp_tokens WHERE user_id=? ORDER BY id DESC""",
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def revoke_mcp_token(user_id: int, token_id: int) -> bool:
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE mcp_tokens SET is_active=0 WHERE id=? AND user_id=? AND is_active=1",
            (token_id, user_id),
        )
        return cursor.rowcount > 0


def add_mcp_audit_log(
    user_id: int,
    token_id: int | None,
    tool_name: str,
    arguments: str,
    success: bool,
    error: str = "",
    duration_ms: int = 0,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO mcp_audit_log
               (user_id, token_id, tool_name, arguments, success, error, duration_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id, token_id, tool_name, arguments[:12000], 1 if success else 0,
                (error or "")[:2000], max(0, int(duration_ms)),
                beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )


def list_mcp_audit_log(user_id: int, limit: int = 100) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, token_id, tool_name, success, error, duration_ms, created_at
               FROM mcp_audit_log WHERE user_id=? ORDER BY id DESC LIMIT ?""",
            (user_id, max(1, min(int(limit), 500))),
        ).fetchall()
    return [dict(row) for row in rows]
