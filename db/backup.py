"""SQLite 在线备份与保留策略。

用 sqlite3 的 backup API 做一致性快照：即使 bot 正在跑（WAL 模式），
也能拿到一个完整可用的副本，不需要停容器、也不用手工拷 -wal/-shm。
备份文件默认落在数据库同级的 ``backups/`` 目录，即 ``./data/backups``，
随 ``./data:/app/data`` 一起留在宿主机上。
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from core.storage import human_size

DEFAULT_KEEP = 7
_PREFIX = "bot-"
_SUFFIX = ".db"

__all__ = [
    "DEFAULT_KEEP",
    "backup_database",
    "default_backup_dir",
    "human_size",
    "list_backups",
    "prune_backups",
    "restore_hint",
]


def default_backup_dir(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)) or ".", "backups")


def _snapshot(src_path: str, dst_path: str) -> None:
    """在线快照：源库只读打开，目标库写完后原子改名。"""
    tmp_path = dst_path + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    # mode=ro 保证不会因为备份动作改动源库；WAL 下的读事务不阻塞写入
    uri = Path(os.path.abspath(src_path)).as_uri() + "?mode=ro"
    src = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        dst = sqlite3.connect(tmp_path)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    os.replace(tmp_path, dst_path)


def backup_database(db_path: str, *, backup_dir: str | None = None,
                    keep: int = DEFAULT_KEEP, name: str | None = None) -> dict:
    """生成一份备份并按 ``keep`` 清理旧文件。

    返回 {path, size, created, keep, removed:[...], total}
    """
    db_path = os.path.abspath(db_path)
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"数据库不存在：{db_path}")
    backup_dir = backup_dir or default_backup_dir(db_path)
    os.makedirs(backup_dir, exist_ok=True)

    stamp = name or time.strftime("%Y%m%d-%H%M%S")
    dst_path = os.path.join(backup_dir, f"{_PREFIX}{stamp}{_SUFFIX}")
    _snapshot(db_path, dst_path)

    removed = prune_backups(backup_dir, keep=keep)
    return {
        "path": dst_path,
        "size": os.path.getsize(dst_path),
        "created": stamp,
        "keep": keep,
        "removed": removed,
        "total": len(list_backups(backup_dir)),
    }


def list_backups(backup_dir: str) -> list[str]:
    """按时间从旧到新列出备份文件。"""
    if not os.path.isdir(backup_dir):
        return []
    names = [n for n in os.listdir(backup_dir)
             if n.startswith(_PREFIX) and n.endswith(_SUFFIX)]
    names.sort()
    return [os.path.join(backup_dir, n) for n in names]


def prune_backups(backup_dir: str, *, keep: int = DEFAULT_KEEP) -> list[str]:
    """只保留最近 ``keep`` 份，返回被删掉的文件路径。"""
    if keep <= 0:
        return []
    backups = list_backups(backup_dir)
    stale = backups[:-keep] if len(backups) > keep else []
    removed = []
    for path in stale:
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass
    return removed


def restore_hint(db_path: str, backup_path: str) -> str:
    """给用户看的恢复步骤（含 WAL 残留文件清理）。"""
    return (
        "恢复步骤：\n"
        "1) 停止容器（docker compose stop / 群晖「停止」）；\n"
        f"2) 删除 {db_path} 以及同目录下的 {os.path.basename(db_path)}-wal、"
        f"{os.path.basename(db_path)}-shm；\n"
        f"3) 把 {backup_path} 复制为 {db_path}；\n"
        "4) 启动容器，任务列表与断点应恢复到备份时刻。"
    )
