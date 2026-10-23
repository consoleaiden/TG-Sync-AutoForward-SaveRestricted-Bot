"""统一降级策略链的离线测试（不连 Telegram）。

覆盖：Bot 直连 → UserBot 转发 → UserBot 读+Bot 写 → 下载上传，
以及能力判定缓存、noforwards 跳过、UserBot FloodWait 熔断、限流节奏。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon import errors

from core.forwarder import (
    ST_BOT,
    ST_DOWNLOAD,
    ST_UB_COPY,
    ST_UB_FORWARD,
    Forwarder,
)
from core.rate_limiter import RateLimiter

SRC = -1001111111111      # 私有源频道
TGT = -1002222222222      # 目标频道


class FakeMessage:
    def __init__(self, msg_id=1, text="", media=None, grouped_id=None):
        self.id = msg_id
        self.text = text
        self.media = media
        self.grouped_id = grouped_id


class FakeEntity:
    def __init__(self, noforwards=False, username=None, title="源频道"):
        self.noforwards = noforwards
        self.username = username
        self.title = title


class FakeClient:
    """按需模拟 Bot / UserBot 的各种能力与失败方式。"""

    def __init__(self, name, *, resolvable=False, entity=None,
                 forward_result=None, forward_error=None,
                 send_error=None, messages=None):
        self.name = name
        self.resolvable = resolvable
        self.entity = entity
        self.forward_result = forward_result
        self.forward_error = forward_error
        self.send_error = send_error
        self.messages = messages or {}
        self.forward_calls = []
        self.send_file_calls = []
        self.send_message_calls = []

    # —— 实体解析 ——
    async def get_input_entity(self, peer):
        if not self.resolvable:
            raise ValueError(f"Cannot find any entity corresponding to {peer}")
        return peer

    async def get_entity(self, peer):
        if self.entity is None:
            raise ValueError(f"Cannot find any entity corresponding to {peer}")
        return self.entity

    # —— 转发 ——
    async def forward_messages(self, entity, messages, from_peer=None, **kwargs):
        self.forward_calls.append({
            "entity": entity,
            "messages": list(messages) if isinstance(messages, (list, tuple)) else [messages],
            "from_peer": from_peer,
            "drop_author": kwargs.get("drop_author"),
        })
        if not self.resolvable:
            # 真实 Telethon 里 from_peer 解析不出来就是这个异常
            raise ValueError(f"Cannot find any entity corresponding to {from_peer}")
        if self.forward_error:
            raise self.forward_error
        if callable(self.forward_result):
            return self.forward_result(messages)
        if self.forward_result is not None:
            return self.forward_result
        ids = messages if isinstance(messages, (list, tuple)) else [messages]
        sent = [FakeMessage(msg_id=1000 + int(i)) for i in ids]
        return sent if isinstance(messages, (list, tuple)) else sent[0]

    # —— 发送 ——
    async def send_file(self, entity, file, **kwargs):
        self.send_file_calls.append({"entity": entity, "file": file, "kwargs": kwargs})
        if self.send_error:
            raise self.send_error
        return FakeMessage(msg_id=555)

    async def send_message(self, entity, text, **kwargs):
        self.send_message_calls.append({"entity": entity, "text": text, "kwargs": kwargs})
        if self.send_error:
            raise self.send_error
        return FakeMessage(msg_id=556)

    # —— 读取 ——
    async def get_messages(self, chat, ids=None, **kwargs):
        if ids is None:
            return []
        if isinstance(ids, (list, tuple)):
            return [self.messages.get(int(i)) for i in ids]
        return self.messages.get(int(ids))

    async def __call__(self, request):
        return None


def make_forwarder(*, bot=None, userbot=None, transfer=None, rate_limit=None):
    config = {
        "transfer": {"enable_fast_transfer": False, **(transfer or {})},
        "rate_limit": {"forward_interval": [0, 0], "batch_pause_every": 0,
                       **(rate_limit or {})},
    }
    rl = RateLimiter(config)

    async def _no_wait(kind="default"):
        return None

    rl.wait = _no_wait  # 测试里不真的 sleep
    forwarder = Forwarder(bot or FakeClient("bot"), userbot or FakeClient("ub"),
                          config, rl)
    return forwarder


class TestStrategyChain(unittest.IsolatedAsyncioTestCase):
    async def test_bot_direct_wins_when_bot_can_read(self):
        bot = FakeClient("bot", resolvable=True, entity=FakeEntity())
        ub = FakeClient("ub", resolvable=True, entity=FakeEntity())
        fw = make_forwarder(bot=bot, userbot=ub)

        result = await fw.forward_message(SRC, 5, TGT, mode="copy")

        self.assertIsNotNone(result)
        self.assertEqual(len(bot.forward_calls), 1)
        self.assertTrue(bot.forward_calls[0]["drop_author"])
        self.assertEqual(ub.forward_calls, [])
        self.assertEqual(fw.strategy_counts[ST_BOT], 1)

    async def test_private_source_falls_to_userbot_forward_without_download(self):
        # Bot 完全解析不到私有源
        bot = FakeClient("bot", resolvable=False, entity=None)
        ub = FakeClient("ub", resolvable=True, entity=FakeEntity())
        fw = make_forwarder(bot=bot, userbot=ub)

        result = await fw.forward_message(SRC, 5, TGT, mode="copy")

        self.assertIsNotNone(result)
        self.assertEqual(len(ub.forward_calls), 1, "应由 UserBot 直接转发")
        self.assertTrue(ub.forward_calls[0]["drop_author"], "copy 模式应去掉来源标记")
        self.assertEqual(bot.send_file_calls, [], "不应下载/重传")
        self.assertEqual(fw.strategy_counts[ST_UB_FORWARD], 1)

    async def test_forward_mode_keeps_author(self):
        bot = FakeClient("bot", resolvable=False)
        ub = FakeClient("ub", resolvable=True, entity=FakeEntity())
        fw = make_forwarder(bot=bot, userbot=ub)

        await fw.forward_message(SRC, 5, TGT, mode="forward")

        self.assertEqual(len(ub.forward_calls), 1)
        self.assertFalse(ub.forward_calls[0]["drop_author"])

    async def test_userbot_cannot_post_falls_back_to_bot_write(self):
        bot = FakeClient("bot", resolvable=False)
        ub = FakeClient("ub", resolvable=True, entity=FakeEntity(),
                        forward_error=errors.ChatWriteForbiddenError(request=None))
        fw = make_forwarder(bot=bot, userbot=ub)
        fw.userbot_messages = {5: FakeMessage(5, text="hello")}
        ub.messages = {5: FakeMessage(5, text="hello")}

        result = await fw.forward_message(SRC, 5, TGT, mode="copy")

        self.assertEqual(result, 556, "应回退到 Bot 写（文本走 send_message）")
        self.assertEqual(len(ub.forward_calls), 1)
        self.assertEqual(fw.strategy_counts[ST_UB_COPY], 1)
        self.assertEqual(fw.strategy_counts[ST_DOWNLOAD], 0)

    async def test_userbot_write_block_cached_for_target(self):
        bot = FakeClient("bot", resolvable=False)
        ub = FakeClient("ub", resolvable=True, entity=FakeEntity(),
                        forward_error=errors.ChatWriteForbiddenError(request=None))
        fw = make_forwarder(bot=bot, userbot=ub)
        ub.messages = {i: FakeMessage(i, text=f"m{i}") for i in (1, 2, 3)}

        for mid in (1, 2, 3):
            await fw.forward_message(SRC, mid, TGT, mode="copy")

        self.assertEqual(len(ub.forward_calls), 1,
                         "同一目标被判定无发言权后，后续消息不应再撞 UserBot")

    async def test_download_strategy_when_both_writes_fail(self):
        bot = FakeClient("bot", resolvable=False, send_error=errors.ChatWriteForbiddenError(request=None))
        ub = FakeClient("ub", resolvable=True, entity=FakeEntity(),
                        forward_error=errors.ChatWriteForbiddenError(request=None))
        fw = make_forwarder(bot=bot, userbot=ub)
        ub.messages = {5: FakeMessage(5, text="hello")}

        calls = []

        async def fake_download(source_chat_id, msg_id, target_chat_id, topic_id, userbot=None):
            calls.append(msg_id)
            return 999

        fw._try_userbot_download_bot_upload = fake_download

        result = await fw.forward_message(SRC, 5, TGT, mode="copy")

        self.assertEqual(result, 999)
        self.assertEqual(calls, [5])
        self.assertEqual(fw.strategy_counts[ST_DOWNLOAD], 1)

    async def test_private_chat_target_keeps_bot_send(self):
        """私聊目标维持原有 Bot 发送（>300MB 切片规则也在这里），不走 UserBot 转发。"""
        bot = FakeClient("bot", resolvable=False)
        ub = FakeClient("ub", resolvable=True, entity=FakeEntity())
        fw = make_forwarder(bot=bot, userbot=ub)
        ub.messages = {5: FakeMessage(5, text="hi")}

        result = await fw.forward_message(SRC, 5, 777, mode="copy", caller="private")

        self.assertEqual(ub.forward_calls, [])
        self.assertEqual(result, 556)

    async def test_album_userbot_forwarded_as_one_unit(self):
        bot = FakeClient("bot", resolvable=False)
        ub = FakeClient("ub", resolvable=True, entity=FakeEntity())
        fw = make_forwarder(bot=bot, userbot=ub)

        result = await fw.forward_album(SRC, [11, 12, 13], TGT, mode="copy")

        self.assertEqual(len(result), 3)
        self.assertEqual(len(ub.forward_calls), 1, "整组应一次转发")
        self.assertEqual(ub.forward_calls[0]["messages"], [11, 12, 13])


class TestCapabilityCache(unittest.IsolatedAsyncioTestCase):
    async def test_bot_unreadable_cached_between_messages(self):
        bot = FakeClient("bot", resolvable=False)
        ub = FakeClient("ub", resolvable=True, entity=FakeEntity())
        fw = make_forwarder(bot=bot, userbot=ub)

        await fw.forward_message(SRC, 1, TGT, mode="copy")
        await fw.forward_message(SRC, 2, TGT, mode="copy")

        self.assertEqual(len(bot.forward_calls), 1,
                         "判定 Bot 读不到后，第二条不应再尝试策略1")
        self.assertEqual(len(ub.forward_calls), 2)

    async def test_noforwards_skips_userbot_forward(self):
        # Bot 能解析实体并看到 noforwards=True，于是连 UserBot 原生转发都不必试
        bot = FakeClient("bot", resolvable=True, entity=FakeEntity(noforwards=True),
                         forward_error=errors.ChatForwardsRestrictedError(request=None))
        ub = FakeClient("ub", resolvable=True, entity=FakeEntity(noforwards=True),
                        forward_error=errors.ChatForwardsRestrictedError(request=None))
        fw = make_forwarder(bot=bot, userbot=ub)
        ub.messages = {i: FakeMessage(i, text=f"m{i}") for i in (1, 2)}

        await fw.forward_message(SRC, 1, TGT, mode="copy")
        await fw.forward_message(SRC, 2, TGT, mode="copy")

        self.assertEqual(ub.forward_calls, [],
                         "源禁止转发时不应浪费 UserBot 调用")
        self.assertTrue(fw._noforwards, "应记住 noforwards，避免每条消息重复探测")

    async def test_userbot_forward_can_be_disabled(self):
        bot = FakeClient("bot", resolvable=False)
        ub = FakeClient("ub", resolvable=True, entity=FakeEntity())
        fw = make_forwarder(bot=bot, userbot=ub,
                            transfer={"userbot_forward": False})
        ub.messages = {5: FakeMessage(5, text="hello")}

        result = await fw.forward_message(SRC, 5, TGT, mode="copy")

        self.assertEqual(ub.forward_calls, [])
        self.assertEqual(result, 556)
        self.assertEqual(fw.strategy_counts[ST_UB_COPY], 1)


class TestUserbotFloodGuard(unittest.IsolatedAsyncioTestCase):
    async def test_flood_waits_then_disables_after_max(self):
        bot = FakeClient("bot", resolvable=False)
        ub = FakeClient("ub", resolvable=True, entity=FakeEntity(),
                        forward_error=errors.FloodWaitError(request=None, capture=0))
        fw = make_forwarder(bot=bot, userbot=ub,
                            transfer={"userbot_max_flood": 2,
                                      "userbot_flood_buffer": 0})
        ub.messages = {5: FakeMessage(5, text="hello")}

        result = await fw.forward_message(SRC, 5, TGT, mode="copy")

        self.assertEqual(len(ub.forward_calls), 2, "第 1 次等待重试，第 2 次触发熔断")
        self.assertTrue(fw._ub_forward_disabled)
        self.assertEqual(result, 556, "熔断后改走 UserBot 读 + Bot 写")
        self.assertEqual(fw.strategy_counts[ST_UB_COPY], 1)

    async def test_disabled_userbot_not_called_again(self):
        bot = FakeClient("bot", resolvable=False)
        ub = FakeClient("ub", resolvable=True, entity=FakeEntity())
        fw = make_forwarder(bot=bot, userbot=ub)
        fw._ub_forward_disabled = True
        ub.messages = {9: FakeMessage(9, text="x")}

        await fw.forward_message(SRC, 9, TGT, mode="copy")

        self.assertEqual(ub.forward_calls, [])


class TestRateLimiterUserbot(unittest.IsolatedAsyncioTestCase):
    async def test_userbot_wait_uses_own_counter(self):
        rl = RateLimiter({"rate_limit": {
            "forward_interval": [0, 0],
            "userbot_forward_interval": [0, 0],
            "userbot_forward_pause_every": 0,
        }})
        await rl.wait(kind="userbot")
        await rl.wait(kind="userbot")
        await rl.wait()
        self.assertEqual(rl._userbot_count, 2)
        self.assertEqual(rl._count, 1)

    async def test_defaults_exist(self):
        rl = RateLimiter({"rate_limit": {}})
        self.assertEqual(len(rl.userbot_forward_interval), 2)
        self.assertGreaterEqual(rl.userbot_forward_interval[0], 1)


class TestStrategySummary(unittest.IsolatedAsyncioTestCase):
    async def test_summary_only_lists_hit_strategies(self):
        fw = make_forwarder()
        fw.strategy_counts[ST_BOT] = 3
        fw.strategy_counts[ST_UB_FORWARD] = 2
        text = fw.strategy_summary()
        self.assertIn("Bot直接 3", text)
        self.assertIn("UserBot转发 2", text)
        self.assertNotIn("下载+上传", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
