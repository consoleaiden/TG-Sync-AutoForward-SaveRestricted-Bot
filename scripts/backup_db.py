"""命令行备份数据库（可在容器内执行，不影响 bot 运行）。

用法：
    python scripts/backup_db.py                     # 备份到 data/backups，保留 7 份
    python scripts/backup_db.py --keep 14           # 调整保留份数
    docker compose exec bot python scripts/backup_db.py
    docker compose run --rm bot python scripts/backup_db.py --db /app/data/bot.db

恢复：停容器 → 删掉 bot.db / bot.db-wal / bot.db-shm → 把备份复制成 bot.db → 启容器。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.storage import resolve_db_path, audit_db_path, format_audit
from db.backup import backup_database, human_size, restore_hint


def main() -> int:
    parser = argparse.ArgumentParser(description="备份 SQLite 任务数据库")
    parser.add_argument("--db", default=None,
                        help="数据库路径（默认取 TG_DB_PATH / config.yaml storage.db_path）")
    parser.add_argument("--backup-dir", default=None, help="备份目录（默认数据库同级 backups/）")
    parser.add_argument("--keep", type=int, default=7, help="保留最近 N 份（默认 7，0=不清理）")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    args = parser.parse_args()

    db_path = args.db
    if not db_path:
        config = {}
        if os.path.exists(args.config):
            try:
                import yaml
                with open(args.config, encoding="utf-8") as f:
                    config = yaml.safe_load(f) or {}
            except Exception as e:      # 配置文件损坏不影响备份
                print(f"⚠️  读取 {args.config} 失败（忽略）：{e}")
        db_path = resolve_db_path(config)

    print(format_audit(audit_db_path(db_path)))
    try:
        info = backup_database(db_path, backup_dir=args.backup_dir, keep=args.keep)
    except FileNotFoundError:
        print(f"❌ 数据库文件不存在：{os.path.abspath(db_path)}")
        return 1
    except Exception as e:
        print(f"❌ 备份失败：{e}")
        return 1

    print(f"✅ 备份完成：{info['path']}（{human_size(info['size'])}）")
    print(f"   现有备份 {info['total']} 份，保留最近 {info['keep']} 份"
          + (f"，已清理 {len(info['removed'])} 份旧备份" if info["removed"] else ""))
    print()
    print(restore_hint(os.path.abspath(db_path), info["path"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
