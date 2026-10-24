"""克隆并发闸门：与 asyncio.Semaphore 等价，但支持运行中调整上限。

Semaphore 的上限创建后不可变；/bf 命令需要在任务排队时也能把上限
调大/调小，因此用 Condition + 计数实现：
- set_limit 调大时唤醒排队中的任务（重新检查上限）；
- 调小时不中断正在跑的任务，只是不再放行新的，直到 active < limit。
"""
from __future__ import annotations

import asyncio


class CloneGate:
    def __init__(self, limit: int = 1):
        self._limit = max(1, int(limit))
        self._active = 0
        self._cond = asyncio.Condition()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def active(self) -> int:
        return self._active

    @property
    def full(self) -> bool:
        return self._active >= self._limit

    async def set_limit(self, n: int) -> None:
        self._limit = max(1, int(n))
        async with self._cond:
            self._cond.notify_all()

    async def __aenter__(self) -> "CloneGate":
        async with self._cond:
            await self._cond.wait_for(lambda: self._active < self._limit)
            self._active += 1
        return self

    async def __aexit__(self, *exc) -> bool:
        async with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()
        return False
