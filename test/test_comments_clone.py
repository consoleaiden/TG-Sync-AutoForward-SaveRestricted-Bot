"""评论区克隆的离线测试：hint/all/off 三种模式、无评论帖跳过、失败计数。"""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.syncer import Syncer
from db import models
from db.database import Database

SRC = -1001
TGT = -1002
DISC_ID = -1000001234567  # utils.get_peer_id(PeerChannel(1234567))


def post(i, text, has_comments=False):
    replies = None
    if has_comments:
        replies = SimpleNamespace(comments=True, discussion_id=1234567)
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
            items = list(self.comments.get(reply_to, []))

            async def gen_comments():
                for m in items:
                    yield m
            return gen_comments()

        async def gen():
            for m in self.msgs:
                if m.id > offset_id:
                    yield m
        return gen()


class RecordingForwarder:
    """记录 (源聊天, 消息ID) 的转发桩；可指定失败对。"""

    def __init__(self, syncer=None, task_id=None, fail_pairs=()):
        self.syncer = syncer
        self.task_id = task_id
        self.calls = []
        self.fail_pairs = set(fail_pairs)
        self._seq = 1000

    async def forward_message(self, src_chat, msg_id, tgt_chat, mode,
                              topic=None, caller=None, userbot=None):
        self.calls.append((src_chat, msg_id))
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


if __name__ == "__main__":
    unittest.main()
