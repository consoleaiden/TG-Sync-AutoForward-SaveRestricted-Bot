"""定位帖子的讨论组（评论区）并保证 UserBot 可读。

「频道帖子 ID → 组内顶层消息 ID」的映射只有服务端知道，对应
messages.getDiscussionMessage；拿到映射后就能直接在组内按 top_msg_id 翻页：

- 私有讨论组只要 UserBot 在组内就能读（不依赖频道的 getReplies 权限）；
- 读到的 message.id 就是组内 ID，转发时不必再猜 chat。

不依赖数据库，Telegram 交互集中在三个小函数里，便于用假 client 单测。
"""
from __future__ import annotations

import logging

from telethon import errors, functions, utils
from telethon.tl.types import Channel, PeerChannel

logger = logging.getLogger("tg_forward_bot.discussion")


def peer_id_of(channel_id: int | None) -> int:
    """裸 channel_id → -100 前缀 peer id（失败返回 0）。"""
    try:
        return utils.get_peer_id(PeerChannel(int(channel_id)))
    except Exception:
        return 0


async def resolve_discussion(client, source_chat_id: int, post,
                             cache: dict[int, int]) -> tuple[int, int | None]:
    """返回 (讨论组 chat_id, 组内顶层消息 ID)；无法定位时 (0, None)。

    cache 以源频道为键缓存讨论组 ID（频道级信息，跨帖子复用）；
    组内顶层 ID 是帖子级信息，无法缓存，每帖一次 RPC。
    """
    disc_id = cache.get(source_chat_id)
    if disc_id is None:
        did = getattr(getattr(post, "replies", None), "discussion_id", None)
        disc_id = peer_id_of(did) if did else 0
        if not disc_id:
            disc_id = await _linked_chat_id(client, source_chat_id)
        cache[source_chat_id] = disc_id

    if not disc_id:
        return 0, None
    return disc_id, await _discussion_top_id(client, source_chat_id, post.id)


async def _linked_chat_id(client, source_chat_id: int) -> int:
    """兜底：从频道全量信息里取 linked_chat_id（顶帖可能没带 replies）。"""
    try:
        entity = await client.get_entity(source_chat_id)
        if not isinstance(entity, Channel):
            return 0
        full = await client(functions.channels.GetFullChannelRequest(entity))
        linked = getattr(full.full_chat, "linked_chat_id", None)
        return peer_id_of(linked) if linked else 0
    except Exception as e:
        logger.debug("解析频道 %s 关联讨论组失败: %s", source_chat_id, e)
        return 0


async def _discussion_top_id(client, source_chat_id: int,
                             post_id: int) -> int | None:
    """频道帖子 → 讨论组里的顶层消息 ID；None 表示该帖没有讨论串。"""
    try:
        r = await client(functions.messages.GetDiscussionMessageRequest(
            peer=source_chat_id, msg_id=int(post_id)))
    except Exception as e:
        logger.debug("取帖子 %s 的讨论消息失败: %s", post_id, e)
        return None
    msgs = getattr(r, "messages", None) or []
    if not msgs:
        return None
    return min(int(m.id) for m in msgs)     # 取法与 Telethon _get_comment_data 一致


async def ensure_member(client, disc_id: int, cache: set[int], *,
                        join: bool) -> bool:
    """确保 UserBot 在讨论组里；返回「是否可读」。可读结果按讨论组缓存。"""
    if not disc_id:
        return False
    if disc_id in cache:
        return True

    try:
        await client(functions.channels.GetParticipantRequest(disc_id, "me"))
        cache.add(disc_id)
        return True
    except errors.UserNotParticipantError:
        pass
    except Exception as e:                  # 组被删 / 被封 / 无权限
        logger.info("讨论组 %s 不可读: %s: %s", disc_id, type(e).__name__, e)
        return False

    if not join:
        return False
    try:
        await client(functions.channels.JoinChannelRequest(disc_id))
        logger.info("UserBot 已自动加入讨论组 %s（评论区克隆）", disc_id)
    except errors.UserAlreadyParticipantError:
        pass
    except errors.InviteRequestSentError:
        logger.info("讨论组 %s 需管理员审批，本次先按频道侧读取", disc_id)
        return False
    except Exception as e:
        logger.warning("加入讨论组 %s 失败: %s: %s", disc_id, type(e).__name__, e)
        return False

    cache.add(disc_id)
    return True
