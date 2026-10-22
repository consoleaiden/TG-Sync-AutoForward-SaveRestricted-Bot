import logging
import os

import aiosqlite

logger = logging.getLogger("tg_forward_bot.db")

# tasks 表基础结构（新库直接带全部列；老库通过 _migrate 补列）
_TASKS_DDL = """
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL,
        source_chat_id INTEGER NOT NULL,
        source_topic_id INTEGER,
        target_chat_id INTEGER NOT NULL,
        target_topic_id INTEGER,
        mode TEXT DEFAULT 'copy',
        status TEXT DEFAULT 'running',
        last_synced_msg_id INTEGER DEFAULT 0,
        start_msg_id INTEGER DEFAULT 0,
        start_date TEXT,
        total_count INTEGER DEFAULT 0,
        done_count INTEGER DEFAULT 0,
        fail_count INTEGER DEFAULT 0,
        skip_count INTEGER DEFAULT 0,
        source_title TEXT,
        target_title TEXT,
        last_error TEXT,
        notify_chat_id INTEGER,
        notify_msg_id INTEGER,
        created_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        finished_at TIMESTAMP
    );
"""

# 老库补列：(列名, 列定义)。只增不改，保证升级不丢数据。
_TASKS_MIGRATIONS: list[tuple[str, str]] = [
    ("start_msg_id", "INTEGER DEFAULT 0"),
    ("start_date", "TEXT"),
    ("total_count", "INTEGER DEFAULT 0"),
    ("done_count", "INTEGER DEFAULT 0"),
    ("fail_count", "INTEGER DEFAULT 0"),
    ("skip_count", "INTEGER DEFAULT 0"),
    ("source_title", "TEXT"),
    ("target_title", "TEXT"),
    ("last_error", "TEXT"),
    ("notify_chat_id", "INTEGER"),
    ("notify_msg_id", "INTEGER"),
    ("created_by", "INTEGER"),
    ("finished_at", "TIMESTAMP"),
]


class Database:
    def __init__(self, db_path: str, *, wal: bool = True):
        self.db_path = db_path
        self.wal = wal
        self.db: aiosqlite.Connection | None = None

    async def init(self):
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        await self._apply_pragmas()
        await self._create_tables()

    async def _apply_pragmas(self):
        """让库在「容器被 kill / 掉电」场景下也不易损坏。

        * WAL：写事务不阻塞读，进程被强杀后下次打开自动回放；
        * synchronous=NORMAL：WAL 下的推荐值，进程崩溃不丢已提交事务；
        * busy_timeout：备份脚本与 bot 同时读写时自动等锁，而不是立刻报
          database is locked；
        * foreign_keys：message_map.task_id 的外键约束真正生效。
        """
        if self.wal:
            await self.db.execute("PRAGMA journal_mode=WAL")
            await self.db.execute("PRAGMA synchronous=NORMAL")
        await self.db.execute("PRAGMA busy_timeout=5000")
        await self.db.execute("PRAGMA foreign_keys=ON")
        await self.db.commit()

    async def checkpoint(self):
        """把 WAL 内容合并回主库文件（关闭/备份前调用，减少 -wal 体积）。"""
        if not self.db:
            return
        try:
            await self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await self.db.commit()
        except Exception as e:      # 非 WAL 模式或只读挂载时忽略
            logger.debug("wal_checkpoint 跳过: %s", e)

    async def _create_tables(self):
        await self.db.executescript(_TASKS_DDL + """
            CREATE TABLE IF NOT EXISTS message_map (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                source_msg_id INTEGER NOT NULL,
                target_msg_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_message_map_task
                ON message_map(task_id, source_msg_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, type);
        """)
        await self._migrate()
        await self.db.commit()

    async def _migrate(self):
        """为旧库补齐新增列（幂等）。"""
        cursor = await self.db.execute("PRAGMA table_info(tasks)")
        existing = {row["name"] for row in await cursor.fetchall()}
        added = []
        for name, ddl in _TASKS_MIGRATIONS:
            if name not in existing:
                await self.db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
                added.append(name)
        if added:
            logger.info("数据库迁移完成，tasks 表新增列: %s", ", ".join(added))

    async def close(self):
        if self.db:
            await self.checkpoint()
            await self.db.close()
            self.db = None
            logger.info("数据库已关闭：%s", self.db_path)
