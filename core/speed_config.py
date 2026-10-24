"""克隆速度阈值：解析 /sd 命令参数并写回 config.yaml 的 rate_limit 段。

RateLimiter.wait() 每次调用都重新读取 config.yaml（热更新），所以这里
只需要把新值写进文件即可立即生效，无需重启进程。

写回采用「手术式」文本替换而不是 yaml.safe_dump 全量重写，避免破坏
用户配置里的注释和排版；键不存在时插到 rate_limit: 段落末尾。
"""
import re

# 目标账号 -> config 键名映射
TARGET_KEYS = {
    "bot": {
        "interval": "forward_interval",
        "pause_every": "batch_pause_every",
        "pause_time": "batch_pause_time",
    },
    "ub": {
        "interval": "userbot_forward_interval",
        "pause_every": "userbot_forward_pause_every",
        "pause_time": "userbot_forward_pause_time",
    },
}

# 预设档位（bot 的 safe / ub 的 safe 与 config.example.yaml 默认值一致）
PRESETS = {
    "bot": {
        "max": {"forward_interval": [0, 0],
                "batch_pause_every": 200,
                "batch_pause_time": [10, 20]},
        "fast": {"forward_interval": [0.3, 0.8],
                 "batch_pause_every": 100,
                 "batch_pause_time": [20, 40]},
        "safe": {"forward_interval": [1, 2],
                 "batch_pause_every": 50,
                 "batch_pause_time": [30, 60]},
    },
    "ub": {
        "fast": {"userbot_forward_interval": [1.5, 3],
                 "userbot_forward_pause_every": 30,
                 "userbot_forward_pause_time": [45, 90]},
        "safe": {"userbot_forward_interval": [3, 6],
                 "userbot_forward_pause_every": 25,
                 "userbot_forward_pause_time": [60, 120]},
    },
}

_INTERVAL_RE = re.compile(r"^(\d+(?:\.\d+)?)(?:\s*[-~]\s*(\d+(?:\.\d+)?))?$")
_PAUSE_RE = re.compile(r"^(\d{1,5})\s+(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)$")

# /sd 覆盖值在 app_settings 表里的键（JSON 数组格式存储，兼容旧版「{键: 值}」）
OVERRIDE_KEY = "rate_limit_overrides"


def normalize_overrides(raw) -> dict:
    """把存储层取出的覆盖值规范化为 {config键: 新值}。

    历史版本存的是 dict；现行版本存 [键1, 键2, ...]，值从 PRESETS 反查，
    避免在数据库里重复维护档位数值。
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, list):
        merged: dict = {}
        for name in raw:
            for target_presets in PRESETS.values():
                preset = target_presets.get(str(name).lower())
                if preset:
                    merged.update(preset)
        return merged
    return {}


def merge_overrides(old: dict, updates: dict) -> dict:
    """合并覆盖值；同名键以新值为准（部分覆盖是合法状态，不做档位联想）。"""
    merged = dict(old)
    merged.update(updates)
    return merged


def _fmt(value) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def parse_speed_args(arg: str):
    """解析 /sd 参数。

    返回 ("show", None) / ("apply", {config_key: value}) / ("error", 提示)。
    """
    parts = arg.split()
    if not parts:
        return ("show", None)

    target = parts[0].lower()
    if target not in TARGET_KEYS:
        return ("error", f"未知对象「{parts[0]}」，只能是 bot 或 ub")

    keys = TARGET_KEYS[target]
    rest = parts[1:]
    if not rest:
        return ("error", f"缺少参数，例如：/sd {target} fast 或 /sd {target} 0.3-0.8")

    head = rest[0].lower()
    if head in PRESETS[target]:
        return ("apply", dict(PRESETS[target][head]))

    if head == "pause":
        if len(rest) < 2:
            return ("error", f"用法：/sd {target} pause <条数> <a>-<b>，"
                             f"如 /sd {target} pause 100 20-40")
        m = _PAUSE_RE.match(" ".join(rest[1:]))
        if not m:
            return ("error", "pause 参数格式应为：条数 + 秒数区间，如 100 20-40")
        every = int(m.group(1))
        lo, hi = float(m.group(2)), float(m.group(3))
        if every <= 0:
            return ("error", "条数必须大于 0（设为 0 表示不休息，不推荐）")
        if lo < 0 or hi < lo:
            return ("error", "休息秒数区间不合法，应为 a-b 且 b ≥ a ≥ 0")
        return ("apply", {keys["pause_every"]: every,
                          keys["pause_time"]: [lo, hi]})

    m = _INTERVAL_RE.match("".join(rest))
    if not m:
        return ("error", f"无法识别「{' '.join(rest)}」，"
                         f"应为秒数或区间，如 0.5 或 0.3-0.8")
    lo = float(m.group(1))
    hi = float(m.group(2)) if m.group(2) else lo
    if lo < 0 or hi < lo:
        return ("error", "间隔秒数不合法，应为 a-b 且 b ≥ a ≥ 0")
    if lo == 0:
        return ("error", "间隔不能为 0（Bot 全速请用 /sd bot max，"
                         "它保留了批次休息兜底）")
    return ("apply", {keys["interval"]: [lo, hi]})


def update_rate_limit_text(text: str, updates: dict) -> str:
    """在 config.yaml 文本中就地更新 rate_limit 段的键，保留注释与排版。"""
    lines = text.splitlines(keepends=True)
    start = next(
        (i for i, ln in enumerate(lines)
         if re.match(r"^rate_limit\s*:", ln)), None)

    block = [f"  {k}: {_fmt(v)}\n" for k, v in updates.items()]
    if start is None:
        if lines and not text.endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        return "".join(lines) + "\nrate_limit:\n" + "".join(block)

    # 段落终点：下一个顶格且非注释的键
    end = start + 1
    while end < len(lines) and (
            not lines[end].strip()
            or lines[end].lstrip().startswith("#")
            or lines[end][:1] in (" ", "\t")):
        end += 1

    for key, value in updates.items():
        pat = re.compile(
            r"^(\s*)%s\s*:[^#]*(#.*)?$" % re.escape(key))
        found = False
        for i in range(start + 1, end):
            m = pat.match(lines[i])
            if m:
                indent = m.group(1) or "  "
                comment = (m.group(2) or "").rstrip()
                if comment:
                    comment = " " + comment
                if not lines[i].endswith("\n"):
                    lines[i] += "\n"
                lines[i] = f"{indent}{key}: {_fmt(value)}{comment}\n"
                found = True
                break
        if not found:
            lines.insert(end, f"  {key}: {_fmt(value)}\n")
            end += 1
    return "".join(lines)


def apply_speed(path: str, updates: dict) -> None:
    """把更新写进 config.yaml（保留其余内容）。"""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    new_text = update_rate_limit_text(text, updates)
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)
