"""评论区克隆的离线测试：触发词、组侧/频道侧读取、正序、幂等、失败计数。"""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

from telethon import errors

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.syncer import Syncer
from db import models
from db.database import Database

SRC = -1001
TGT = -1002
DISC_ID = -1000001234567  # utils.get_peer_id(PeerChannel(1234567))


def post(i, text, has_comments=False, replies_count=3):
    replies = None
    if has_comments:
        replies = SimpleNamespace(comments=True, replies=replies_count,
                                  discussion_id=1234567)
    return SimpleNamespace(id=i, grouped_id=None, text=text, media=None,
                           replies=replies)


def comment(i):
    return SimpleNamespace(id=5000 + i, grouped_id=None, text=f"评论{i}",
                           media=None, chat_id=DISC_ID)


class CommentUserbot:
    """支持按 reply_to（帖子 ID）返回评论区的假 UserBot。"""

    def __init__(self, msgs, comments=None):
        self.msgs = msgs
        self.comments = comments or {}
        self.comment_reads = []
        self.joined = []

    async def get_messages(self, chat_id, **kwargs):
        if "offset_date" in kwargs:
            return SimpleNamespace(id=0)
        if "ids" in kwargs:
            wanted = set(kwargs["ids"])
            return [m for m in self.msgs if m.id in wanted]
        return SimpleNamespace(total=len(self.msgs))

    async def get_entity(self, chat_id):
        return SimpleNamespace(restricted=False, restriction_reason=[])

    def iter_messages(self, chat_id, reverse=False, offset_id=0,
                      reply_to=None, **kwargs):
        if reply_to is not None:
            self.comment_reads.append(reply_to)
            # 真实 messages.getReplies 按 id 倒序返回，桩必须一致，
            # 否则「正序落地」这条断言测不出东西
            items = sorted(self.comments.get(reply_to, []),
                           key=lambda m: m.id, reverse=True)

            async def gen_comments():
                for m in items:
                    yield m
            return gen_comments()

        async def gen():
            for m in self.msgs:
                if m.id > offset_id:
                    yield m
        return gen()


class DiscussionUserbot(CommentUserbot):
    """能响应讨论组相关 RPC 的假 UserBot（GetParticipant / JoinChannel /
    GetDiscussionMessage），用于验证「自动进讨论组 + 组侧优先」链路。"""

    def __init__(self, msgs, comments=None, *, member=True, group_error=None,
                 top_id=9001):
        super().__init__(msgs, comments)
        self.member = member
        self.group_error = group_error
        self.top_id = top_id
        self.requests = []

    async def __call__(self, request):
        name = type(request).__name__
        self.requests.append(name)
        if name == "GetParticipantRequest":
            if not self.member:
                raise errors.UserNotParticipantError(None)
            return SimpleNamespace(participant=SimpleNamespace())
        if name == "JoinChannelRequest":
            self.member = True
            self.joined.append(request.channel)
            return SimpleNamespace()
        if name == "GetDiscussionMessageRequest":
            return SimpleNamespace(
                messages=[SimpleNamespace(id=self.top_id)], chats=[], users=[])
        raise AssertionError(f"未预期的请求: {name}")

    def iter_messages(self, chat_id, reverse=False, offset_id=0,
                      reply_to=None, **kwargs):
        # 组侧读取失败（私有组 / 机器人被踢）用它模拟
        if self.group_error is not None and chat_id == DISC_ID:
            self.comment_reads.append(reply_to)   # 记一笔：确实尝试过组侧
            raise self.group_error
        return super().iter_messages(chat_id, reverse=reverse,
                                     offset_id=offset_id, reply_to=reply_to,
                                     **kwargs)


class RecordingForwarder:
    """记录 (源聊天, 消息ID) 的转发桩；可指定失败对。"""

    def __init__(self, syncer=None, task_id=None, fail_pairs=()):
        self.syncer = syncer
        self.task_id = task_id
        self.calls = []
        self.fail_pairs = set(fail_pairs)
        self.marker_flags = []
        self._seq = 1000

    async def forward_message(self, src_chat, msg_id, tgt_chat, mode,
                              topic=None, caller=None, userbot=None,
                              with_fail_marker=True):
        self.calls.append((src_chat, msg_id))
        self.marker_flags.append(with_fail_marker)
        self._seq += 1
        return None if (src_chat, msg_id) in self.fail_pairs else self._seq

    async def forward_album(self, src_chat, ids, tgt_chat, mode,
                            topic=None, caller=None, userbot=None):
        out = []
        for i in ids:
            self.calls.append((src_chat, i))
            self._seq += 1
            out.append(self._seq)
        return out

    def should_prefetch(self):
        return False

    async def prefetch_media(self, *a, **kw):
        return None

    def cleanup_prefetch(self):
        return None

    def strategy_summary(self):
        return ""

    def diagnostics_summary(self):
        return ""


class RecordingBot:
    def __init__(self):
        self.texts = []

    async def send_message(self, chat, text, **kwargs):
        self.texts.append(text)
        return SimpleNamespace(id=len(self.texts))

    async def edit_message(self, chat, mid, text, **kwargs):
        self.texts.append(text)
        return SimpleNamespace(id=mid)

    async def get_entity(self, chat_id):
        return SimpleNamespace(restricted=False, restriction_reason=[])


class CommentsCloneTest(unittest.IsolatedAsyncioTestCase):
    def _msgs(self):
        return [
            post(1, "干货合集 评论区获取 👇", has_comments=True),
            post(2, "普通消息，无评论区"),
            post(3, "闲聊帖", has_comments=True),
        ]

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmp, "c.db"))
        await self.db.init()
        self.comments = {1: [comment(1), comment(2), comment(3)],
                         3: [comment(4)]}
        self.userbot = CommentUserbot(self._msgs(), self.comments)
        self.bot = RecordingBot()
        self.config = {
            "clone": {"batch_size": 4, "progress_notify_every": 5,
                      "stall_alert_seconds": 0},
            "rate_limit": {"forward_interval": [0, 0], "batch_pause_every": 999,
                           "batch_pause_time": [0, 0]},
        }
        self.syncer = Syncer(bot=self.bot, userbot=self.userbot,
                             db=self.db, config=self.config)

    async def asyncTearDown(self):
        await self.db.close()
        for f in os.listdir(self.tmp):
            try:
                os.remove(os.path.join(self.tmp, f))
            except OSError:
                pass
        os.rmdir(self.tmp)

    async def test_hint_mode_clones_marked_post_comments(self):
        task_id = await models.create_task(self.db, "sync", SRC, TGT, "copy")
        fwd = RecordingForwarder(self.syncer, task_id)
        self.syncer.forwarder = fwd

        await self.syncer.start_sync(task_id, SRC, TGT, "copy",
                                     notify_chat_id=-999,
                                     userbot=self.userbot)

        # 顺序：先克隆帖子 1，再克隆其 3 条评论，然后才是帖子 2/3
        self.assertEqual(fwd.calls, [
            (SRC, 1),
            (DISC_ID, 5001), (DISC_ID, 5002), (DISC_ID, 5003),
            (SRC, 2), (SRC, 3),
        ])
        self.assertEqual(self.userbot.comment_reads, [1],
                         "只有命中提示词的帖子才进评论区")
        final = self.bot.texts[-1]
        self.assertIn("评论区: 1 个帖子克隆了 3 条评论", final)
        # 评论不计入任务 done（覆盖率分母保持源频道口径）
        t = await models.get_task(self.db, task_id)
        self.assertEqual(t["done_count"], 3)

    async def test_no_hint_no_comment_reads(self):
        task_id = await models.create_task(self.db, "sync", SRC, TGT, "copy")
        fwd = RecordingForwarder(self.syncer, task_id)
        self.syncer.forwarder = fwd

        await self.syncer.start_sync(task_id, SRC, TGT, "copy",
                                     notify_chat_id=-999,
                                     userbot=self.userbot)

        # hint 模式下，帖子 3 开了评论区但没有提示词 → 不进；
        # 帖子 1 命中提示词 → 进
        self.assertEqual(self.userbot.comment_reads, [1])
        self.assertIn("1 个帖子克隆了 3 条评论", self.bot.texts[-1])

    async def test_all_mode_clones_every_comment_thread(self):
        self.syncer.comments_mode = "all"
        task_id = await models.create_task(self.db, "sync", SRC, TGT, "copy")
        fwd = RecordingForwarder(self.syncer, task_id)
        self.syncer.forwarder = fwd

        await self.syncer.start_sync(task_id, SRC, TGT, "copy",
                                     notify_chat_id=-999,
                                     userbot=self.userbot)

        self.assertEqual(self.userbot.comment_reads, [1, 3],
                         "all 模式下凡开评论区的帖子都进")
        self.assertIn("2 个帖子克隆了 4 条评论", self.bot.texts[-1])

    async def test_off_mode_disables_comments(self):
        self.syncer.comments_mode = "off"
        task_id = await models.create_task(self.db, "sync", SRC, TGT, "copy")
        fwd = RecordingForwarder(self.syncer, task_id)
        self.syncer.forwarder = fwd

        await self.syncer.start_sync(task_id, SRC, TGT, "copy",
                                     notify_chat_id=-999,
                                     userbot=self.userbot)

        self.assertEqual(self.userbot.comment_reads, [])
        self.assertEqual(fwd.calls, [(SRC, 1), (SRC, 2), (SRC, 3)])

    async def test_hint_without_comments_flag_is_skipped(self):
        # 命中提示词但帖子没开评论区：不报错、不进评论区
        msgs = [post(1, "内容 评论区获取", has_comments=False)]
        self.userbot = CommentUserbot(msgs, {})
        self.syncer.userbot = self.userbot
        task_id = await models.create_task(self.db, "sync", SRC, TGT, "copy")
        fwd = RecordingForwarder(self.syncer, task_id)
        self.syncer.forwarder = fwd

        await self.syncer.start_sync(task_id, SRC, TGT, "copy",
                                     notify_chat_id=-999,
                                     userbot=self.userbot)

        self.assertEqual(self.userbot.comment_reads, [])
        self.assertEqual(fwd.calls, [(SRC, 1)])

    async def test_comment_failures_reported(self):
        task_id = await models.create_task(self.db, "sync", SRC, TGT, "copy")
        fwd = RecordingForwarder(self.syncer, task_id,
                                 fail_pairs={(DISC_ID, 5002), (DISC_ID, 5003)})
        self.syncer.forwarder = fwd

        await self.syncer.start_sync(task_id, SRC, TGT, "copy",
                                     notify_chat_id=-999,
                                     userbot=self.userbot)

        self.assertIn("评论区: 1 个帖子克隆了 1 条评论，失败 2 条",
                      self.bot.texts[-1])

    async def test_comment_max_caps_cloning(self):
        self.syncer.comment_max = 2
        task_id = await models.create_task(self.db, "sync", SRC, TGT, "copy")
        fwd = RecordingForwarder(self.syncer, task_id)
        self.syncer.forwarder = fwd

        await self.syncer.start_sync(task_id, SRC, TGT, "copy",
                                     notify_chat_id=-999,
                                     userbot=self.userbot)

        comment_calls = [c for c in fwd.calls if c[0] == DISC_ID]
        self.assertEqual(len(comment_calls), 2, "comment_max 截断评论克隆")

    # ── 新增：触发词泛化 ──────────────────────────────────────────────

    async def test_hint_matches_plain_comment_area(self):
        """正文只写「评论区」（旧实现只认「评论区获取」）也要触发。"""
        msgs = [post(1, "更多内容请见评论区", has_comments=True)]
        self.userbot = CommentUserbot(msgs, {1: [comment(1)]})
        self.syncer.userbot = self.userbot
        task_id = await models.create_task(self.db, "sync", SRC, TGT, "copy")
        fwd = RecordingForwarder(self.syncer, task_id)
        self.syncer.forwarder = fwd

        await self.syncer.start_sync(task_id, SRC, TGT, "copy",
                                     notify_chat_id=-999,
                                     userbot=self.userbot)

        self.assertEqual(self.userbot.comment_reads, [1])
        self.assertEqual(fwd.calls, [(SRC, 1), (DISC_ID, 5001)])

    async def test_hint_matches_with_zero_width(self):
        """关键词被零宽字符/空白拆开也要命中（反爬频道的常见写法）。"""
        msgs = [post(1, "评\u200b论\u200b区 自取", has_comments=True)]
        self.userbot = CommentUserbot(msgs, {1: [comment(1)]})
        self.syncer.userbot = self.userbot
        task_id = await models.create_task(self.db, "sync", SRC, TGT, "copy")
        fwd = RecordingForwarder(self.syncer, task_id)
        self.syncer.forwarder = fwd

        await self.syncer.start_sync(task_id, SRC, TGT, "copy",
                                     notify_chat_id=-999,
                                     userbot=self.userbot)

        self.assertEqual(self.userbot.comment_reads, [1])

    async def test_comment_hints_list_from_config(self):
        """comment_hints 列表任一命中即触发。"""
        cfg = {"clone": {"batch_size": 4, "stall_alert_seconds": 0,
                         "comment_hints": ["看置顶", "自取"]},
               "rate_limit": {"forward_interval": [0, 0], "batch_pause_every": 999,
                              "batch_pause_time": [0, 0]}}
        syncer = Syncer(bot=self.bot, userbot=self.userbot, db=self.db,
                        config=cfg)
        self.assertEqual(syncer.comment_hints, ("看置顶", "自取"))
        msgs = [post(1, "资料自取", has_comments=True)]
        self.userbot = CommentUserbot(msgs, {1: [comment(1)]})
        fwd = RecordingForwarder(syncer)
        syncer.forwarder = fwd

        task_id = await models.create_task(self.db, "sync", SRC, TGT, "copy")
        await syncer.start_sync(task_id, SRC, TGT, "copy",
                                notify_chat_id=-999, userbot=self.userbot)
        self.assertEqual(self.userbot.comment_reads, [1])

    # ── 新增：顺序 / 幂等 / 失败语义 ─────────────────────────────────

    async def test_comments_forwarded_in_chronological_order(self):
        """服务端倒序返回，落地必须是正序（对话顺序）。"""
        msgs = [post(1, "内容 评论区", has_comments=True)]
        comments = [comment(i) for i in range(1, 6)]
        self.userbot = CommentUserbot(msgs, {1: comments})
        self.syncer.userbot = self.userbot
        task_id = await models.create_task(self.db, "sync", SRC, TGT, "copy")
        fwd = RecordingForwarder(self.syncer, task_id)
        self.syncer.forwarder = fwd

        await self.syncer.start_sync(task_id, SRC, TGT, "copy",
                                     notify_chat_id=-999,
                                     userbot=self.userbot)

        ids = [c[1] for c in fwd.calls if c[0] == DISC_ID]
        self.assertEqual(ids, sorted(ids), "评论应按 id 正序落地")

    async def test_fail_marker_not_counted_as_comment(self):
        """comment 转发强制 with_fail_marker=False，失败不会被当成成功。"""
        msgs = [post(1, "内容 评论区", has_comments=True)]
        self.userbot = CommentUserbot(msgs, {1: [comment(1), comment(2)]})
        self.syncer.userbot = self.userbot
        task_id = await models.create_task(self.db, "sync", SRC, TGT, "copy")
        fwd = RecordingForwarder(
            self.syncer, task_id,
            fail_pairs={(DISC_ID, 5001), (DISC_ID, 5002)})
        self.syncer.forwarder = fwd

        await self.syncer.start_sync(task_id, SRC, TGT, "copy",
                                     notify_chat_id=-999,
                                     userbot=self.userbot)

        comment_flags = [f for (chat, _), f in zip(fwd.calls, fwd.marker_flags)
                         if chat == DISC_ID]
        self.assertTrue(comment_flags and not any(comment_flags),
                        "评论转发必须关闭 #fail2forward 标记")
        self.assertIn("1 个帖子克隆了 0 条评论，失败 2 条", self.bot.texts[-1])

    async def test_thread_skipped_when_post_failed(self):
        """帖子本身没落地，就不该搬它的评论（避免无主评论）。"""
        msgs = [post(1, "内容 评论区", has_comments=True)]
        self.userbot = CommentUserbot(msgs, {1: [comment(1)]})
        self.syncer.userbot = self.userbot
        task_id = await models.create_task(self.db, "sync", SRC, TGT, "copy")
        fwd = RecordingForwarder(self.syncer, task_id, fail_pairs={(SRC, 1)})
        self.syncer.forwarder = fwd

        await self.syncer.start_sync(task_id, SRC, TGT, "copy",
                                     notify_chat_id=-999,
                                     userbot=self.userbot)

        self.assertEqual(self.userbot.comment_reads, [])

    async def test_dedupe_skips_done_thread(self):
        """同一帖第二次调用直接跳过（幂等）。"""
        task_id = await models.create_task(self.db, "sync", SRC, TGT, "copy")
        fwd = RecordingForwarder(self.syncer, task_id)
        self.syncer.forwarder = fwd
        p = post(1, "内容 评论区", has_comments=True)
        self.userbot = CommentUserbot([p], {1: [comment(1), comment(2)]})

        first = await self.syncer._clone_post_comments(
            task_id, p, SRC, TGT, "copy", None, self.userbot)
        second = await self.syncer._clone_post_comments(
            task_id, p, SRC, TGT, "copy", None, self.userbot)

        self.assertEqual(first, (2, 0, False))
        self.assertEqual(second, (0, 0, False))
        self.assertEqual(len(fwd.calls), 2, "第二次不应重复转发")

    # ── 新增：讨论组优先 / 入组策略 ─────────────────────────────────

    async def test_group_side_read_preferred(self):
        """能进组时从组侧读（贴子 ID → 组内顶层消息 ID）。"""
        msgs = [post(1, "内容 评论区", has_comments=True)]
        ub = DiscussionUserbot(msgs, {9001: [comment(1)]}, top_id=9001)
        self.syncer.userbot = ub
        task_id = await models.create_task(self.db, "sync", SRC, TGT, "copy")
        fwd = RecordingForwarder(self.syncer, task_id)
        self.syncer.forwarder = fwd

        await self.syncer.start_sync(task_id, SRC, TGT, "copy",
                                     notify_chat_id=-999, userbot=ub)

        self.assertEqual(ub.comment_reads, [9001], "应从讨论组侧读取")
        self.assertIn("GetDiscussionMessageRequest", ub.requests)
        self.assertEqual(fwd.calls, [(SRC, 1), (DISC_ID, 5001)])

    async def test_group_side_falls_back_to_channel(self):
        """组侧读不到时回落频道侧 getReplies，不静默丢评论。"""
        msgs = [post(1, "内容 评论区", has_comments=True)]
        ub = DiscussionUserbot(msgs, {1: [comment(1), comment(2)]},
                              top_id=9001,
                              group_error=errors.ChannelPrivateError(None))
        self.syncer.userbot = ub
        task_id = await models.create_task(self.db, "sync", SRC, TGT, "copy")
        fwd = RecordingForwarder(self.syncer, task_id)
        self.syncer.forwarder = fwd

        await self.syncer.start_sync(task_id, SRC, TGT, "copy",
                                     notify_chat_id=-999, userbot=ub)

        self.assertEqual(ub.comment_reads, [9001, 1], "组侧失败后要回落频道侧")
        self.assertEqual(len([c for c in fwd.calls if c[0] == DISC_ID]), 2)

    async def test_join_only_for_hint_mode(self):
        """comment_join=auto 时只有 hint 命中帖才自动入组。"""
        msgs = [post(1, "内容 评论区", has_comments=True)]
        ub = DiscussionUserbot(msgs, {1: [comment(1)]}, member=False,
                               top_id=9001)
        self.syncer.userbot = ub
        task_id = await models.create_task(self.db, "sync", SRC, TGT, "copy")
        self.syncer.forwarder = RecordingForwarder(self.syncer, task_id)

        await self.syncer.start_sync(task_id, SRC, TGT, "copy",
                                     notify_chat_id=-999, userbot=ub)
        self.assertIn("JoinChannelRequest", ub.requests, "hint 命中应自动入组")

        # all 模式：不主动入组，只读得到就读
        ub2 = DiscussionUserbot(msgs, {1: [comment(1)]}, member=False,
                                top_id=9001)
        syncer2 = Syncer(bot=self.bot, userbot=ub2, db=self.db,
                         config={"clone": {"batch_size": 4,
                                           "stall_alert_seconds": 0,
                                           "comments_mode": "all"},
                                 "rate_limit": {"forward_interval": [0, 0],
                                                "batch_pause_every": 999,
                                                "batch_pause_time": [0, 0]}})
        syncer2.forwarder = RecordingForwarder(syncer2)
        task2 = await models.create_task(self.db, "sync", SRC, TGT, "copy")
        await syncer2.start_sync(task2, SRC, TGT, "copy",
                                 notify_chat_id=-999, userbot=ub2)
        self.assertNotIn("JoinChannelRequest", ub2.requests,
                         "all 模式不应无差别入群")


if __name__ == "__main__":
    unittest.main()
