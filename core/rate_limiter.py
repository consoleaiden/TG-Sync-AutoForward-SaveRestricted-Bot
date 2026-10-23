"""全局速率限制器，控制转发频率，防止触发 FloodWaitError。"""
import asyncio
import random
import yaml

def _get_dynamic_rate_limit(fallback_config):
    try:
        with open("config.yaml", encoding="utf-8") as f:
            c = yaml.safe_load(f)
            return c.get("rate_limit", {})
    except Exception:
        return fallback_config.get("rate_limit", {})

class RateLimiter:
    def __init__(self, config: dict):
        self.fallback_config = config
        rl = _get_dynamic_rate_limit(self.fallback_config)
        self.forward_interval = rl.get("forward_interval", [2, 5])
        self.batch_pause_every = rl.get("batch_pause_every", 50)
        self.batch_pause_time = rl.get("batch_pause_time", [30, 60])
        self.flood_wait_multiplier = rl.get("flood_wait_multiplier", 2)
        self.max_flood_wait = rl.get("max_flood_wait", 300)
        # UserBot 是用户账号，被风控的代价远高于 Bot，单独一套更慢的节奏
        self.userbot_forward_interval = rl.get("userbot_forward_interval", [3, 6])
        self.userbot_forward_pause_every = rl.get("userbot_forward_pause_every", 25)
        self.userbot_forward_pause_time = rl.get("userbot_forward_pause_time", [60, 120])

        self._count = 0
        self._userbot_count = 0
        self._interval_scale = 1.0  # FloodWait 后翻倍
        self._lock = asyncio.Lock()

    def _current_limits(self) -> dict:
        """读取最新限流配置（支持运行时热更新 config.yaml）。"""
        return _get_dynamic_rate_limit(self.fallback_config)

    async def wait(self, kind: str = "default"):
        """每次转发前调用，自动控制速率。

        kind="userbot" 时使用更保守的 UserBot 节奏（`userbot_forward_*`），
        用于「UserBot 直接转发」这条策略，降低用户账号被限流的概率。
        """
        async with self._lock:
            rl = self._current_limits()
            self.flood_wait_multiplier = rl.get("flood_wait_multiplier", self.flood_wait_multiplier)
            self.max_flood_wait = rl.get("max_flood_wait", self.max_flood_wait)

            if kind == "userbot":
                self._userbot_count += 1
                every = max(0, int(rl.get("userbot_forward_pause_every",
                                          self.userbot_forward_pause_every)))
                if every and self._userbot_count % every == 0:
                    pause = rl.get("userbot_forward_pause_time",
                                   self.userbot_forward_pause_time)
                    await asyncio.sleep(random.uniform(*pause))
                else:
                    interval = rl.get("userbot_forward_interval",
                                      self.userbot_forward_interval)
                    await asyncio.sleep(random.uniform(*interval) * self._interval_scale)
                return

            forward_interval = rl.get("forward_interval", self.forward_interval)
            batch_pause_every = rl.get("batch_pause_every", self.batch_pause_every)
            batch_pause_time = rl.get("batch_pause_time", self.batch_pause_time)

            self._count += 1
            # 批次休息
            if self._count % batch_pause_every == 0:
                pause = random.uniform(*batch_pause_time)
                await asyncio.sleep(pause)
            else:
                interval = random.uniform(*forward_interval) * self._interval_scale
                await asyncio.sleep(interval)

    def on_flood_wait(self):
        """遇到 FloodWait 后调用，增大间隔。"""
        self._interval_scale *= self.flood_wait_multiplier

    def reset_scale(self):
        """重置间隔倍率。"""
        self._interval_scale = 1.0
