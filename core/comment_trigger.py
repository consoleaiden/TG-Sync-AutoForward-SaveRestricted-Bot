"""评论区触发判定：把「这条帖子要不要搬评论区」抽成纯函数。

历史实现要求正文包含完整提示词「评论区获取」，只写「评论区」「看评论区」
「评论区自取」的帖子全部漏搬；这里统一做归一化 + 多关键词匹配，并兼容旧的
单字符串配置。

不依赖 Telegram 网络与数据库，便于单元测试。
"""
from __future__ import annotations

import re
import unicodedata

# 默认触发词：出现「评论区」三个字就搬（「评论区获取」是它的超集，旧配置自动兼容）
DEFAULT_HINTS: tuple[str, ...] = ("评论区",)

# 零宽 / BOM / 软连字符：反爬频道常把它们塞进关键词中间来躲匹配
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)
_WS_RE = re.compile(r"\s+")


def normalize_text(text: str | None) -> str:
    """全角→半角、去掉零宽字符与所有空白，让「评 论 区」也能命中。"""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text)).translate(_INVISIBLE)
    return _WS_RE.sub("", s)


def parse_hints(raw_hints, raw_hint) -> tuple[str, ...]:
    """优先 comment_hints（列表或逗号串），否则回落到旧键 comment_hint。

    旧实现把 comment_hint 设为空串等价于「关闭」，这是隐式语义、极易误配；
    新实现里空值统一回到默认值，要关请用 comments_mode: off。
    """
    items: list[str] = []
    if isinstance(raw_hints, (list, tuple)):
        items = [str(x) for x in raw_hints]
    elif isinstance(raw_hints, str) and raw_hints.strip():
        items = re.split(r"[,，|]", raw_hints)
    if not items and isinstance(raw_hint, str) and raw_hint.strip():
        items = [raw_hint]

    cleaned: list[str] = []
    for it in items:
        n = normalize_text(it)
        if n and n not in cleaned:
            cleaned.append(n)
    return tuple(cleaned) or DEFAULT_HINTS


def match_comment_hint(text: str | None, hints: tuple[str, ...]) -> str | None:
    """命中返回关键词（写日志 / 汇报用），否则 None。"""
    norm = normalize_text(text)
    if not norm:
        return None
    for h in hints:
        if h in norm:
            return h
    return None


def should_clone_comments(post, *, mode: str, hints: tuple[str, ...],
                          min_replies: int = 0) -> tuple[bool, str]:
    """统一判定，返回 (是否搬, 原因)。原因会进日志与汇报，便于排查漏搬。

    原因取值：off / no-comments / too-few(N) / all / hint:<关键词> / no-hint
    """
    if mode == "off":
        return False, "off"

    replies = getattr(post, "replies", None)
    if not replies or not getattr(replies, "comments", False):
        return False, "no-comments"

    count = int(getattr(replies, "replies", 0) or 0)
    if min_replies > 0 and count < min_replies:
        return False, f"too-few({count})"

    if mode == "all":                       # 全量连带：只要开了评论区就搬
        return True, "all"

    text = getattr(post, "text", None) or getattr(post, "message", "") or ""
    hit = match_comment_hint(text, hints)
    return (True, f"hint:{hit}") if hit else (False, "no-hint")
