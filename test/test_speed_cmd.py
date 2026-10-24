"""/sd 速度阈值命令：参数解析、覆盖值持久化、config.yaml 手术式写回、命令回显。"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.handlers import register_handlers
from core.monitor import MonitorManager
from core.rate_limiter import RateLimiter
from core.speed_config import (
    OVERRIDE_KEY, merge_overrides, normalize_overrides,
    parse_speed_args, update_rate_limit_text, apply_speed,
)
from db.database import Database

SAMPLE_YAML = """# 主配置
bot_token: "xxx"

rate_limit:
  batch_size: 100
  forward_interval: [1, 2] # 每条转发间隔范围（秒）
  batch_pause_every: 50 # 每转发N条后休息
  batch_pause_time: [30, 60] # 休息时间范围（秒）
  # UserBot 独立节奏
  userbot_forward_interval: [3, 6]
  userbot_forward_pause_every: 25

transfer:
  enable_fast_transfer: true # 保持注释不动
"""


class ParseSpeedArgsTest(unittest.TestCase):
    def test_empty_is_show(self):
        self.assertEqual(parse_speed_args(""), ("show", None))

    def test_bot_preset(self):
        action, payload = parse_speed_args("bot fast")
        self.assertEqual(action, "apply")
        self.assertEqual(payload["forward_interval"], [0.3, 0.8])
        self.assertEqual(payload["batch_pause_every"], 100)

    def test_bot_max(self):
        _, payload = parse_speed_args("bot max")
        self.assertEqual(payload["forward_interval"], [0, 0])

    def test_ub_safe(self):
        _, payload = parse_speed_args("ub safe")
        self.assertEqual(payload["userbot_forward_interval"], [3, 6])

    def test_interval_single_and_range(self):
        _, p1 = parse_speed_args("bot 0.5")
        self.assertEqual(p1, {"forward_interval": [0.5, 0.5]})
        _, p2 = parse_speed_args("ub 2-4")
        self.assertEqual(p2, {"userbot_forward_interval": [2.0, 4.0]})

    def test_pause(self):
        _, p = parse_speed_args("ub pause 30 45-90")
        self.assertEqual(p, {"userbot_forward_pause_every": 30,
                             "userbot_forward_pause_time": [45.0, 90.0]})

    def test_errors(self):
        for arg in ("foo 1-2", "bot 0", "bot 5-1", "bot pause", "bot abc",
                    "ub pause 0 10-20", "ub"):
            action, payload = parse_speed_args(arg)
            self.assertEqual(action, "error", arg)


class UpdateRateLimitTextTest(unittest.TestCase):
    def test_replaces_existing_keys_and_keeps_comments(self):
        out = update_rate_limit_text(SAMPLE_YAML, {
            "forward_interval": [0.3, 0.8],
            "batch_pause_every": 100,
        })
        self.assertIn("forward_interval: [0.3, 0.8] # 每条转发间隔范围（秒）\n", out)
        self.assertIn("batch_pause_every: 100 # 每转发N条后休息\n", out)
        self.assertIn("# 每条转发间隔范围（秒）", out, "行尾注释应保留")
        self.assertIn("userbot_forward_interval: [3, 6]", out, "未涉及的键不动")
        self.assertIn("enable_fast_transfer: true", out)

    def test_inserts_missing_keys(self):
        out = update_rate_limit_text(SAMPLE_YAML, {
            "userbot_forward_pause_time": [45, 90],
        })
        self.assertIn("userbot_forward_pause_time: [45, 90]\n", out)
        # 插入应仍在 rate_limit 段内（transfer: 之前）
        self.assertLess(out.index("userbot_forward_pause_time"),
                        out.index("transfer:"))

    def test_appends_section_when_missing(self):
        out = update_rate_limit_text("bot_token: \"x\"\n", {"forward_interval": [1, 2]})
        self.assertIn("rate_limit:\n  forward_interval: [1, 2]\n", out)

    def test_indent_follows_original(self):
        out = update_rate_limit_text(SAMPLE_YAML, {"batch_size": 200})
        self.assertIn("  batch_size: 200\n", out)


class ApplySpeedRoundtripTest(unittest.TestCase):
    def test_roundtrip_preserves_rest_of_file(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "config.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(SAMPLE_YAML)
        try:
            apply_speed(path, {"forward_interval": [0.3, 0.8],
                               "batch_pause_time": [20, 40]})
            with open(path, encoding="utf-8") as f:
                out = f.read()
            self.assertIn("forward_interval: [0.3, 0.8]", out)
            self.assertIn("batch_pause_time: [20, 40]", out)
            self.assertIn('bot_token: "xxx"', out)
            self.assertIn("enable_fast_transfer: true", out)
        finally:
            os.remove(path)
            os.rmdir(tmp)


class NormalizeOverridesTest(unittest.TestCase):
    def test_none_and_dict_passthrough(self):
        self.assertEqual(normalize_overrides(None), {})
        self.assertEqual(normalize_overrides({"forward_interval": [1, 2]}),
                         {"forward_interval": [1, 2]})

    def test_legacy_dict(self):
        self.assertEqual(normalize_overrides({"a": 1}), {"a": 1})

    def test_name_list_expands_presets(self):
        ov = normalize_overrides(["fast"])
        self.assertEqual(ov["forward_interval"], [0.3, 0.8])
        self.assertEqual(ov["userbot_forward_interval"], [1.5, 3])

    def test_merge(self):
        merged = merge_overrides({"forward_interval": [1, 2]},
                                 {"forward_interval": [0.5, 0.5]})
        self.assertEqual(merged, {"forward_interval": [0.5, 0.5]})


class RateLimiterOverrideTest(unittest.IsolatedAsyncioTestCase):
    async def test_overrides_take_precedence(self):
        sleeps: list[float] = []

        async def fake_sleep(sec):
            sleeps.append(sec)

        rl = RateLimiter({"rate_limit": {"forward_interval": [1, 2]}})
        rl.set_overrides({"forward_interval": [0.1, 0.1],
                          "batch_pause_every": 2,
                          "batch_pause_time": [0.3, 0.3]})
        with unittest.mock.patch("asyncio.sleep", fake_sleep):
            await rl.wait()
            await rl.wait()  # 第 2 条触发批次休息
        self.assertAlmostEqual(sleeps[0], 0.1)
        self.assertAlmostEqual(sleeps[1], 0.3)


class FakeBot:
    def __init__(self):
        self.handlers = []

    def on(self, event):
        def deco(fn):
            self.handlers.append((event, fn))
            return fn
        return deco


class FakeEvent:
    def __init__(self, text, sender_id=7):
        self.raw_text = text
        self.sender_id = sender_id
        self.is_private = True
        self.replies = []

    async def reply(self, text, **_kw):
        self.replies.append(text)

    async def answer(self, *_a, **_kw):
        pass


class SpeedCommandWiringTest(unittest.IsolatedAsyncioTestCase):
    def _find_speed_handler(self, bot):
        from telethon import events
        for ev, fn in bot.handlers:
            if not isinstance(ev, events.NewMessage):
                continue
            p = getattr(ev, "pattern", None)
            # Telethon 存的是 pattern.match 绑定方法，原串在 __self__.pattern
            src = getattr(getattr(p, "__self__", None), "pattern", "")
            if src and "/sd" in src:
                return fn
        return None

    async def test_sd_show_and_apply(self):
        tmp = tempfile.mkdtemp()
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            with open("config.yaml", "w", encoding="utf-8") as f:
                f.write(SAMPLE_YAML)
            db = Database(os.path.join(tmp, "sd.db"))
            await db.init()
            bot = FakeBot()
            config = {"admin_ids": [7], "rate_limit": {}}
            monitor = MonitorManager(bot, bot, db, config)
            register_handlers(bot, bot, db, config, monitor)
            try:
                fn = self._find_speed_handler(bot)
                self.assertIsNotNone(fn, "未注册 /sd 处理器")

                ev = FakeEvent("/sd")
                await fn(ev)
                self.assertIn("【Bot】每条间隔 1~2 秒", ev.replies[0])
                self.assertIn("【UserBot】每条间隔 3~6 秒", ev.replies[0])

                ev2 = FakeEvent("/sd bot fast")
                await fn(ev2)
                self.assertTrue(ev2.replies[0].startswith("✅ 已更新"), ev2.replies[0])
                self.assertIn("🟢 在线覆盖生效中", ev2.replies[0])
                # 覆盖值应持久化到 app_settings
                from db import models as _models
                raw = await _models.get_setting(db, OVERRIDE_KEY)
                ov = normalize_overrides(json.loads(raw))
                self.assertEqual(ov.get("forward_interval"), [0.3, 0.8])
                self.assertEqual(ov.get("batch_pause_every"), 100)
                # config.yaml 可写时应被顺手同步
                with open("config.yaml", encoding="utf-8") as f:
                    written = f.read()
                self.assertIn("forward_interval: [0.3, 0.8]", written)
                self.assertIn("batch_pause_every: 100", written)
                # 再次 /sd 显示的是生效值（config + 覆盖）
                ev3 = FakeEvent("/sd")
                await fn(ev3)
                self.assertIn("每条间隔 0.3~0.8 秒", ev3.replies[0])
            finally:
                await db.close()
        finally:
            os.chdir(old_cwd)
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    async def test_sd_works_with_readonly_config(self):
        """config.yaml 只读（容器常见挂载方式）时 /sd 仍必须可用。"""
        tmp = tempfile.mkdtemp()
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            cfg = os.path.join(tmp, "config.yaml")
            with open(cfg, "w", encoding="utf-8") as f:
                f.write(SAMPLE_YAML)
            os.chmod(cfg, 0o444)  # 只读
            db = Database(os.path.join(tmp, "sd3.db"))
            await db.init()
            bot = FakeBot()
            config = {"admin_ids": [7], "rate_limit": {}}
            monitor = MonitorManager(bot, bot, db, config)
            register_handlers(bot, bot, db, config, monitor)
            try:
                fn = self._find_speed_handler(bot)
                ev = FakeEvent("/sd bot fast")
                await fn(ev)
                self.assertTrue(ev.replies[0].startswith("✅ 已更新"), ev.replies[0])
                self.assertIn("只读", ev.replies[0], "应提示 config.yaml 只读但不影响")

                # 覆盖值已入库，且重启后 _load_speed_overrides 能恢复
                from db import models as _models
                raw = await _models.get_setting(db, OVERRIDE_KEY)
                self.assertIsNotNone(raw)
                ov = normalize_overrides(json.loads(raw))
                self.assertEqual(ov.get("forward_interval"), [0.3, 0.8])

                # RateLimiter 拿到覆盖值后行为符合预期
                rl = RateLimiter(config)
                rl.set_overrides(ov)
                sleeps: list[float] = []

                async def fake_sleep(sec):
                    sleeps.append(sec)
                with unittest.mock.patch("asyncio.sleep", fake_sleep):
                    await rl.wait()
                self.assertGreaterEqual(sleeps[0], 0.3)
                self.assertLessEqual(sleeps[0], 0.8)
            finally:
                os.chmod(cfg, 0o644)
                await db.close()
        finally:
            os.chdir(old_cwd)
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    async def test_sd_rejects_non_admin(self):
        tmp = tempfile.mkdtemp()
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            with open("config.yaml", "w", encoding="utf-8") as f:
                f.write(SAMPLE_YAML)
            db = Database(os.path.join(tmp, "sd2.db"))
            await db.init()
            bot = FakeBot()
            config = {"admin_ids": [7], "rate_limit": {}}
            monitor = MonitorManager(bot, bot, db, config)
            register_handlers(bot, bot, db, config, monitor)
            try:
                fn = self._find_speed_handler(bot)

                ev = FakeEvent("/sd bot max", sender_id=999)
                await fn(ev)
                self.assertEqual(ev.replies, ["⛔ 无权限"])
                with open("config.yaml", encoding="utf-8") as f:
                    self.assertIn("forward_interval: [1, 2]", f.read())
            finally:
                await db.close()
        finally:
            os.chdir(old_cwd)
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
