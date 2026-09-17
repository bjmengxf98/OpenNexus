"""Conversation-safe upload staging.

Files are stored under ``data/uploads`` instead of the operating-system temp
directory.  Dequeuing only transfers ownership to a chat turn; it must not
delete the file because the assistant may ask a clarification and use the same
attachment in a later turn.
"""
import os
import re
import uuid
from pathlib import Path

_queue: dict = {}  # {uid: [{"name": str, "path": str}]}
_UPLOAD_ROOT = Path(__file__).resolve().parent.parent / "data" / "uploads"


def store(uid, name: str, data: bytes) -> dict:
    """Persist one browser upload and enqueue it for the user's next turn."""
    uid = int(uid)
    user_dir = _UPLOAD_ROOT / str(uid)
    user_dir.mkdir(parents=True, exist_ok=True)
    suffix = re.sub(r"[^A-Za-z0-9.]", "", Path(name or "").suffix)[:16]
    target = user_dir / f"{uuid.uuid4().hex}{suffix}"
    target.write_bytes(data)
    if os.name != "nt":
        target.chmod(0o600)
    item = {"name": name or target.name, "path": str(target)}
    _queue.setdefault(uid, []).append(item)
    return dict(item)


def enqueue(uid, name: str, path: str):
    _queue.setdefault(int(uid), []).append({"name": name, "path": path})


def dequeue_all(uid) -> list:
    return _queue.pop(int(uid), [])


def requeue(uid, files: list[dict]):
    """Return uncommitted files to the front of the user's pending queue."""
    uid = int(uid)
    current = _queue.setdefault(uid, [])
    known = {str(item.get("path") or "") for item in current}
    restored = [dict(item) for item in files if str(item.get("path") or "") not in known]
    _queue[uid] = restored + current


def clear(uid):
    for f in _queue.pop(int(uid), []):
        try:
            os.unlink(f["path"])
        except Exception:
            pass


def remove_by_name(uid, filename: str) -> bool:
    """根据文件名删除指定文件"""
    uid = int(uid)
    if uid not in _queue:
        return False

    files = _queue[uid]
    for i, f in enumerate(files):
        if f["name"] == filename:
            # 删除物理文件
            try:
                os.unlink(f["path"])
            except Exception:
                pass
            # 从队列中移除
            files.pop(i)
            return True

    return False


def peek(uid) -> list:
    return list(_queue.get(int(uid), []))


def delete_paths(paths) -> list[str]:
    """Delete only files owned by the managed upload root."""
    root = _UPLOAD_ROOT.resolve()
    deleted: list[str] = []
    for value in paths:
        try:
            path = Path(str(value or "")).resolve()
            if not path.is_relative_to(root):
                continue
            path.unlink(missing_ok=True)
            deleted.append(str(path))
        except (OSError, RuntimeError, ValueError):
            continue
    return deleted
