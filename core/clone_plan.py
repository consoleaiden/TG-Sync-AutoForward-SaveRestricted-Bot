"""克隆任务的纯逻辑工具：时间解析、进度渲染、批量切分、起点描述。

不依赖 Telegram 网络与数据库，便于单元测试。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

# ── 时间解析 ──────────────────────────────────────────────────────────────

_DATE_PATTERNS = (
    # 2024-01-31 12:30 / 2024-01-31 12:30:45
    (re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?$"), "full"),
    # 2024-01-31
    (re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$"), "date"),
    # 20240131
    (re.compile(r"^(\d{4})(\d{2})(\d{2})$"), "compact"),
    # 20240131 1230
    (re.compile(r"^(\d{4})(\d{2})(\d{2})[ T](\d{2})(\d{2})$"), "compact_full"),
    # 10 位 unix 时间戳
    (re.compile(r"^(\d{10})$"), "ts"),
)


def parse_start_time(text: str, *, tz=None) -> datetime | None:
    """解析用户输入的时间，返回带时区的 datetime（默认本机时区）。

    支持: 2024-01-31 / 2024-01-31 12:30[:45] / 2024/01/31 / 20240131 /
    20240131 1230 / 10 位 unix 时间戳。解析失败返回 None。
    """
    if not text:
        return None
    raw = text.strip()
    if tz is None:
        tz = datetime.now().astimezone().tzinfo

    for pattern, kind in _DATE_PATTERNS:
        m = pattern.match(raw)
        if not m:
            continue
        try:
            if kind == "ts":
                return datetime.fromtimestamp(int(m.group(1)), tz)
            if kind == "compact":
                return datetime(int(m.group(1)), int(m.group(2)),
                                int(m.group(3)), tzinfo=tz)
            if kind == "compact_full":
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                int(m.group(4)), int(m.group(5)), tzinfo=tz)
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if kind == "date":
                return datetime(y, mo, d, tzinfo=tz)
            hh, mm = int(m.group(4)), int(m.group(5))
            ss = int(m.group(6)) if m.group(6) else 0
            return datetime(y, mo, d, hh, mm, ss, tzinfo=tz)
        except ValueError:
            return None
    return None


def format_time(dt: datetime) -> str:
    """统一的人类可读时间（YYYY-MM-DD HH:MM）。"""
    return dt.strftime("%Y-%m-%d %H:%M")


# ── 进度渲染 ──────────────────────────────────────────────────────────────

_BAR_FULL = "▰"
_BAR_EMPTY = "▱"


def progress_bar(done: int, total: int | None, width: int = 10) -> str:
    """生成进度条；总量未知时返回只带计数的占位条。"""
    done = max(0, int(done or 0))
    if not total or total <= 0:
        return f"{_BAR_EMPTY * width}  已处理 {done:,} 条"
    ratio = min(1.0, done / total)
    filled = int(round(ratio * width))
    filled = max(0, min(width, filled))
    bar = _BAR_FULL * filled + _BAR_EMPTY * (width - filled)
    return f"{bar} {ratio * 100:5.1f}%  ({done:,}/{total:,})"


def human_count(n: int | None) -> str:
    if n is None:
        return "未知"
    return f"{int(n):,}"


def format_duration(seconds: float | None) -> str:
    """把秒数格式化成 1h23m / 45m12s 形式。"""
    if seconds is None or seconds < 0:
        return "未知"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def estimate_remaining(done: int, total: int | None,
                       started_at: datetime | None,
                       now: datetime | None = None) -> str:
    """基于已用时间与完成量估算剩余耗时。"""
    if not total or total <= done or not started_at:
        return "未知"
    now = now or datetime.now(started_at.tzinfo)
    elapsed = (now - started_at).total_seconds()
    if elapsed <= 0 or done <= 0:
        return "未知"
    per_item = elapsed / done
    return format_duration(per_item * (total - done))


# ── 批量切分（相册安全）────────────────────────────────────────────────────

def split_album_tail(buffer: list, batch_size: int) -> tuple[list, list]:
    """按 batch_size 切分待处理消息，且不把一个相册拆到两批。

    返回 (可立即处理的批次, 留在缓冲区等待更多消息的尾巴)。
    若缓冲区开头就是一个超过 batch_size 的相册（无法再等），强制按
    batch_size 切，避免无限等待。
    """
    if batch_size <= 0:
        return list(buffer), []
    if len(buffer) <= batch_size:
        return [], list(buffer)

    cut = batch_size
    gid_at_cut = getattr(buffer[cut - 1], "grouped_id", None)
    if gid_at_cut:
        start = cut - 1
        while start > 0 and getattr(buffer[start - 1], "grouped_id", None) == gid_at_cut:
            start -= 1
        # 相册从缓冲区开头就开始 → 它比 batch_size 还大，只能硬切
        cut = batch_size if start == 0 else start
    return list(buffer[:cut]), list(buffer[cut:])


def describe_start(start_msg_id: int = 0, start_date: str | None = None,
                   resumed: bool = False, done: int = 0,
                   last_msg_id: int = 0) -> str:
    """生成起点的人类可读描述。"""
    if resumed:
        extra = f"，已处理 {done:,} 条" if done else ""
        return f"断点续传（从消息 {last_msg_id} 之后继续{extra}）"
    if start_msg_id:
        return f"从指定消息 {start_msg_id} 开始"
    if start_date:
        return f"从 {start_date} 之后开始"
    return "从头开始（最早 → 最新）"


def coerce_start_date(value) -> str | None:
    """把用户输入或已存值统一成 ISO 字符串。"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def parse_stored_date(value) -> datetime | None:
    """从任务中读回 start_date。"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def week_ago(now: datetime | None = None) -> datetime:
    now = now or datetime.now().astimezone()
    return now - timedelta(days=7)
