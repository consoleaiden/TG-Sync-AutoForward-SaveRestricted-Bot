"""通用配置覆盖层：把「命令在线调整过的配置」持久化到 app_settings 表。

设计：
- 覆盖值以 {"段.键": 值} 存在 app_settings（key=config_overrides）里，
  data/bot.db 挂载到宿主机，容器重建不丢；
- 启动时在组件构造之前 apply_overlay 深合并进内存 config dict，
  之后所有组件读到的都是「config.yaml + 覆盖值」的合成结果；
- 优先级：app_settings 覆盖值 > config.yaml > 代码默认值。
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("tg_forward_bot.config_store")

OVERLAY_KEY = "config_overrides"


def parse_value(text: str) -> Any:
    """把命令里的字符串解析成配置值（复用 YAML 标量语法）。

    "900" -> 900，"0.5" -> 0.5，"true" -> True，"[1, 2]" -> [1, 2]，
    解析失败时按原样字符串返回。
    """
    import yaml
    try:
        v = yaml.safe_load(text)
    except yaml.YAMLError:
        return text
    return text if v is None and text else v


async def async_load_overlay(db) -> dict:
    """事件循环内读取覆盖值（推荐入口）。"""
    from db import models
    raw = await models.get_setting(db, OVERLAY_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("config_overrides 不是合法 JSON，已忽略")
        return {}
    return data if isinstance(data, dict) else {}


async def async_save_overlay(db, overlay: dict) -> None:
    from db import models
    await models.set_setting(db, OVERLAY_KEY, json.dumps(overlay, ensure_ascii=False))


def apply_overlay(config: dict, overlay: dict) -> dict:
    """把 {"段.键": 值} 深合并进内存 config dict（就地修改并返回）。

    只认两段式路径（本项目所有段都是一层结构），未知段/键也允许写入，
    便于未来新增配置项时无需改这里。
    """
    for path, value in (overlay or {}).items():
        parts = path.split(".", 1)
        if len(parts) != 2:
            logger.warning("忽略非法覆盖路径: %s", path)
            continue
        section, key = parts
        cfg_section = config.setdefault(section, {})
        if not isinstance(cfg_section, dict):
            logger.warning("覆盖路径 %s 的父级不是映射，已跳过", path)
            continue
        cfg_section[key] = value
    return config
