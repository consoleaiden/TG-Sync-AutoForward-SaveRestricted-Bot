"""数据库操作方法，封装 tasks 和 message_map 的 CRUD。

`tasks` 一行即一个克隆/监控任务，同时充当该任务的进度快照：
- last_synced_msg_id : 已处理到的源消息 ID（断点续传游标）
- start_msg_id/start_date : 用户指定的起点（仅首次启动时生效）
- total_count/done_count/fail_count/skip_count : 进度计数
"""
from db.database import Database

# 参与「克隆」语义（可暂停/续传）的任务类型
CLONE_TASK_TYPES = ("sync", "sync_restricted")


async def create_task(db: Database, task_type: str, source_chat_id: int,
                      target_chat_id: int, mode: str = "copy",
                      source_topic_id: int | None = None,
                      target_topic_id: int | None = None,
                      start_msg_id: int = 0,
                      start_date: str | None = None,
                      source_title: str | None = None,
                      target_title: str | None = None,
                      notify_chat_id: int | None = None,
                      notify_msg_id: int | None = None,
                      created_by: int | None = None) -> int:
    cursor = await db.db.execute(
        """INSERT INTO tasks (type, source_chat_id, source_topic_id,
           target_chat_id, target_topic_id, mode,
           start_msg_id, start_date, source_title, target_title,
           notify_chat_id, notify_msg_id, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (task_type, source_chat_id, source_topic_id,
         target_chat_id, target_topic_id, mode,
         start_msg_id or 0, start_date, source_title, target_title,
         notify_chat_id, notify_msg_id, created_by),
    )
    await db.db.commit()
    return cursor.lastrowid


async def update_task_status(db: Database, task_id: int, status: str):
    if status == "completed":
        await db.db.execute(
            "UPDATE tasks SET status=?, updated_at=CURRENT_TIMESTAMP,"
            " finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, task_id),
        )
    else:
        await db.db.execute(
            "UPDATE tasks SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, task_id),
        )
    await db.db.commit()


async def update_last_synced(db: Database, task_id: int, msg_id: int):
    await db.db.execute(
        "UPDATE tasks SET last_synced_msg_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (msg_id, task_id),
    )
    await db.db.commit()


async def update_progress(db: Database, task_id: int, *,
                          last_msg_id: int | None = None,
                          done_delta: int = 0,
                          fail_delta: int = 0,
                          skip_delta: int = 0,
                          total: int | None = None,
                          last_error: str | None = None) -> None:
    """原子更新进度：计数用增量累加，游标/总量直接覆盖。"""
    sets, args = [], []
    if last_msg_id is not None:
        sets.append("last_synced_msg_id=?")
        args.append(int(last_msg_id))
    if done_delta:
        sets.append("done_count=COALESCE(done_count,0)+?")
        args.append(int(done_delta))
    if fail_delta:
        sets.append("fail_count=COALESCE(fail_count,0)+?")
        args.append(int(fail_delta))
    if skip_delta:
        sets.append("skip_count=COALESCE(skip_count,0)+?")
        args.append(int(skip_delta))
    if total is not None:
        sets.append("total_count=?")
        args.append(int(total))
    if last_error is not None:
        sets.append("last_error=?")
        args.append(last_error[:500])
    if not sets:
        return
    sets.append("updated_at=CURRENT_TIMESTAMP")
    args.append(task_id)
    await db.db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", args)
    await db.db.commit()


async def update_task_titles(db: Database, task_id: int, *,
                             source_title: str | None = None,
                             target_title: str | None = None) -> None:
    sets, args = [], []
    if source_title:
        sets.append("source_title=?")
        args.append(source_title)
    if target_title:
        sets.append("target_title=?")
        args.append(target_title)
    if not sets:
        return
    args.append(task_id)
    await db.db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", args)
    await db.db.commit()


async def update_task_notify(db: Database, task_id: int,
                             chat_id: int, msg_id: int) -> None:
    """记住进度消息的位置，续传/重启后继续编辑同一条消息。"""
    await db.db.execute(
        "UPDATE tasks SET notify_chat_id=?, notify_msg_id=? WHERE id=?",
        (chat_id, msg_id, task_id))
    await db.db.commit()


async def get_task(db: Database, task_id: int) -> dict | None:
    cursor = await db.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_tasks_by_status(db: Database, status: str) -> list[dict]:
    cursor = await db.db.execute("SELECT * FROM tasks WHERE status=?", (status,))
    return [dict(r) for r in await cursor.fetchall()]


async def get_tasks_by_types(db: Database, types: tuple[str, ...],
                             statuses: tuple[str, ...] | None = None) -> list[dict]:
    if not types:
        return []
    placeholders = ",".join("?" * len(types))
    sql = f"SELECT * FROM tasks WHERE type IN ({placeholders})"
    args: list = list(types)
    if statuses:
        sql += f" AND status IN ({','.join('?' * len(statuses))})"
        args.extend(statuses)
    sql += " ORDER BY id DESC"
    cursor = await db.db.execute(sql, args)
    return [dict(r) for r in await cursor.fetchall()]


async def find_resumable_clone(db: Database, source_chat_id: int,
                               target_chat_id: int,
                               source_topic_id: int | None,
                               target_topic_id: int | None) -> dict | None:
    """查找同一源→目标下可继续的克隆任务（未完成且有游标）。"""
    cursor = await db.db.execute(
        """SELECT * FROM tasks
           WHERE type IN ('sync','sync_restricted')
             AND source_chat_id=? AND target_chat_id=?
             AND IFNULL(source_topic_id,0)=IFNULL(?,0)
             AND IFNULL(target_topic_id,0)=IFNULL(?,0)
             AND status IN ('paused','failed','running')
             AND IFNULL(last_synced_msg_id,0) > 0
           ORDER BY id DESC LIMIT 1""",
        (source_chat_id, target_chat_id, source_topic_id, target_topic_id),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def find_monitor_task(db: Database, source_chat_id: int,
                            target_chat_id: int,
                            source_topic_id: int | None,
                            target_topic_id: int | None,
                            statuses: tuple[str, ...] = ("running", "paused")
                            ) -> dict | None:
    """查找同一「源话题 → 目标话题」下已存在的监控任务，用于防止重复添加。"""
    placeholders = ",".join("?" for _ in statuses)
    cursor = await db.db.execute(
        f"""SELECT * FROM tasks
            WHERE type='monitor'
              AND source_chat_id=? AND target_chat_id=?
              AND IFNULL(source_topic_id,0)=IFNULL(?,0)
              AND IFNULL(target_topic_id,0)=IFNULL(?,0)
              AND status IN ({placeholders})
            ORDER BY id DESC LIMIT 1""",
        (source_chat_id, target_chat_id, source_topic_id, target_topic_id,
         *statuses),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_tasks_by_type(db: Database, task_type: str) -> list[dict]:
    """按类型取全部任务（含已完成的），用于监控任务总览。"""
    cursor = await db.db.execute(
        """SELECT * FROM tasks WHERE type=?
           AND status != 'deleted'
           ORDER BY (status='running') DESC, id DESC""",
        (task_type,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def save_message_map(db: Database, task_id: int,
                           source_msg_id: int, target_msg_id: int):
    await db.db.execute(
        "INSERT INTO message_map (task_id, source_msg_id, target_msg_id) VALUES (?,?,?)",
        (task_id, source_msg_id, target_msg_id))
    await db.db.commit()


async def get_all_active_tasks(db: Database) -> list[dict]:
    cursor = await db.db.execute(
        "SELECT * FROM tasks WHERE status IN ('running', 'paused', 'failed') ORDER BY id DESC"
    )
    return [dict(r) for r in await cursor.fetchall()]


async def get_interrupted_clones(db: Database) -> list[dict]:
    """进程重启前仍处于 running 的克隆任务（需要自动续传）。"""
    return await get_tasks_by_types(db, CLONE_TASK_TYPES, ("running",))
