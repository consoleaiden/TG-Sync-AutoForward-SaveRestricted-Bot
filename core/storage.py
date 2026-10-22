"""数据落盘位置解析与持久化自检。

克隆进度、任务游标、消息映射都存在 SQLite（默认 ``data/bot.db``）里。
容器升级镜像会重建容器，**只有挂载到宿主机的目录里的数据才会保留**，
所以这里统一负责三件事：

1. 解析数据库路径（环境变量 > config.yaml > 默认值），方便把库放到任意
   宿主机挂载点；
2. 启动时自检：库文件是否位于「已持久化目录」内，不在就大声告警，
   避免镜像升级后进度凭空消失；
3. 给菜单/文档提供一份可读的存储信息。

判定逻辑不做任何假设：容器内默认认为 ``/app/data``、``/app/sessions``
是持久化的（对应 docker-compose 的挂载），也可用 ``TG_PERSIST_DIRS``
或 config 里的 ``storage.persist_dirs`` 覆盖。
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DB_PATH = "data/bot.db"

ENV_DB_PATH = "TG_DB_PATH"
ENV_PERSIST_DIRS = "TG_PERSIST_DIRS"

# 与 docker-compose.yaml 中的挂载点保持一致
CONTAINER_PERSIST_DIRS = ("/app/data", "/app/sessions")


def resolve_db_path(config: dict | None = None, env: dict | None = None) -> str:
    """数据库文件路径：TG_DB_PATH > config.storage.db_path > data/bot.db。"""
    env = os.environ if env is None else env
    raw = str(env.get(ENV_DB_PATH) or "").strip()
    if not raw:
        storage = (config or {}).get("storage") or {}
        raw = str(storage.get("db_path") or "").strip()
    return raw or DEFAULT_DB_PATH


def _as_bool(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(value)


def wal_enabled(config: dict | None = None) -> bool:
    """是否启用 WAL（默认开；放到 SMB/NFS 网络盘时可关掉改回回滚日志）。"""
    return _as_bool(((config or {}).get("storage") or {}).get("wal"), True)


def backup_settings(config: dict | None = None) -> dict:
    """备份目录与保留份数；目录为空时默认放在数据库同级的 backups/。"""
    storage = (config or {}).get("storage") or {}
    backup_dir = str(storage.get("backup_dir") or "").strip() or None
    try:
        keep = int(storage.get("backup_keep", 7))
    except (TypeError, ValueError):
        keep = 7
    return {"backup_dir": backup_dir, "keep": max(1, keep)}


def is_in_container(env: dict | None = None) -> bool:
    """粗判是否运行在容器内（只用于告警措辞与持久化判定，不影响功能）。"""
    env = os.environ if env is None else env
    if env.get("TG_IN_CONTAINER"):
        return str(env["TG_IN_CONTAINER"]).lower() not in ("0", "false", "no")
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return any(k in cgroup for k in ("docker", "kubepods", "containerd", "podman"))


def persist_dirs(config: dict | None = None, env: dict | None = None,
                 in_container: bool | None = None) -> list[str]:
    """返回「被认为已持久化」的目录列表（容器内才需要判断）。"""
    env = os.environ if env is None else env
    raw = str(env.get(ENV_PERSIST_DIRS) or "").strip()
    dirs: list[str] = []
    if raw:
        dirs = [p.strip() for p in raw.split(",") if p.strip()]
    else:
        storage = (config or {}).get("storage") or {}
        configured = storage.get("persist_dirs") or []
        if isinstance(configured, str):
            configured = [configured]
        dirs = [str(p) for p in configured if str(p).strip()]
    if dirs:
        return [os.path.abspath(p) for p in dirs]
    if in_container is None:
        in_container = is_in_container(env)
    return list(CONTAINER_PERSIST_DIRS) if in_container else []


def _under(path: str, parent: str) -> bool:
    path, parent = os.path.realpath(path), os.path.realpath(parent)
    if path == parent:
        return True
    try:
        return os.path.commonpath([path, parent]) == parent
    except ValueError:      # Windows 跨盘符
        return False


def audit_db_path(db_path: str, *, config: dict | None = None,
                  env: dict | None = None, in_container: bool | None = None) -> dict:
    """汇总数据库落盘情况，供日志与菜单展示。

    ``persisted`` 三种取值：
    * True  —— 位于已挂载到宿主机的目录，镜像升级不丢；
    * False —— 位于容器可写层，重建容器/升级镜像会丢；
    * None  —— 无法判断（非容器运行，或未配置需要判断的目录）。
    """
    env = os.environ if env is None else env
    abs_path = os.path.abspath(db_path)
    if in_container is None:
        in_container = is_in_container(env)
    dirs = persist_dirs(config, env, in_container)

    size = 0
    try:
        size = os.path.getsize(abs_path)
    except OSError:
        size = 0

    persisted: bool | None
    if not in_container:
        persisted = None
    elif not dirs:
        persisted = None
    else:
        persisted = any(_under(abs_path, d) for d in dirs)

    return {
        "path": abs_path,
        "dir": os.path.dirname(abs_path) or ".",
        "existed": os.path.exists(abs_path),
        "size": size,
        "wal": os.path.exists(abs_path + "-wal"),
        "in_container": bool(in_container),
        "persist_dirs": dirs,
        "persisted": persisted,
    }


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def format_audit(audit: dict) -> str:
    """一行摘要，适合直接进日志。"""
    state = {True: "已持久化", False: "未持久化", None: "无需判断"}[audit["persisted"]]
    db_state = "已存在" if audit["existed"] else "新建"
    return (f"数据库 {audit['path']}（{db_state}，"
            f"{human_size(audit['size'])}，{state}）")


def persistence_warning(audit: dict, db_path: str) -> str | None:
    """未持久化时返回一段可执行的中文告警，否则返回 None。"""
    if audit["persisted"] is not False:
        return None
    return (
        "检测到数据库不在宿主机挂载目录内，重建容器/升级镜像会丢失全部克隆进度！\n"
        f"  当前路径：{audit['path']}\n"
        f"  已挂载目录：{', '.join(audit['persist_dirs']) or '（无）'}\n"
        "  修复方式（二选一）：\n"
        f"    1) 把库放回挂载目录：config.yaml 中删除 storage.db_path，"
        f"或设为 data/bot.db（默认），并确保 docker 挂载 ./data:/app/data；\n"
        "    2) 为自定义的库目录补一个挂载：\n"
        f"       docker run -v /宿主机/路径:{audit['dir']} ...\n"
        "  群晖：Container Manager → 项目/容器 → 编辑 → 卷，把目录映射到宿主机共享文件夹。"
    )
