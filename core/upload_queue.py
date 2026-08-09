"""
服务端临时文件队列 - 按用户ID存储待发送文件
"""
import os

_queue: dict = {}  # {uid: [{"name": str, "path": str}]}


def enqueue(uid, name: str, path: str):
    _queue.setdefault(int(uid), []).append({"name": name, "path": path})


def dequeue_all(uid) -> list:
    return _queue.pop(int(uid), [])


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
