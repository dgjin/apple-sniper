"""请求节流：每主机串行 + 最小间隔 + 令牌桶 + 抖动。

刻意拆成「纯计算 :meth:`HostLimiter.plan`」和「真正等待 :meth:`HostLimiter.acquire`」两步：
``plan()`` 只读状态、返回此刻还需要等多少秒，所以可以用**假时钟**做毫秒级单测，
不必让测试真的睡几秒。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass
class _HostState:
    last_request_at: float | None = None
    tokens: float = 0.0
    tokens_at: float | None = None


class HostLimiter:
    """同一主机串行发请求，并叠加最小间隔与平均速率两道闸。

    抖动只**加**不减：任何情况下都不会比配置允许的更快。
    """

    def __init__(
        self,
        *,
        min_interval_seconds: float = 3.0,
        max_requests_per_minute: int = 20,
        jitter_ratio: float = 0.0,
        burst: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if max_requests_per_minute <= 0:
            raise ValueError("max_requests_per_minute 必须为正数")
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self.rate_per_second = max_requests_per_minute / 60.0
        self.jitter_ratio = max(0.0, jitter_ratio)
        self.burst = max(1.0, burst)
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._state: dict[str, _HostState] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def plan(self, host: str) -> float:
        """返回距离「允许发下一次请求」还需等待的秒数（0 表示可以立即发）。"""
        now = self._clock()
        state = self._state.setdefault(host, _HostState())
        if state.tokens_at is None:
            state.tokens_at = now
            state.tokens = self.burst

        delay = 0.0
        if state.last_request_at is not None:
            delay = max(delay, state.last_request_at + self.min_interval_seconds - now)

        tokens = min(self.burst, state.tokens + (now - state.tokens_at) * self.rate_per_second)
        if tokens < 1.0:
            delay = max(delay, (1.0 - tokens) / self.rate_per_second)

        delay = max(0.0, delay)
        return delay + self._jitter(delay)

    async def acquire(self, host: str) -> float:
        """阻塞到可以发请求为止，返回实际等待的秒数。"""
        lock = self._locks.setdefault(host, asyncio.Lock())
        waited = 0.0
        async with lock:
            while True:
                delay = self.plan(host)
                if delay <= 0:
                    break
                await self._sleep(delay)
                waited += delay
            self._commit(host)
        return waited

    def _jitter(self, delay: float) -> float:
        if delay <= 0 or self.jitter_ratio <= 0:
            return 0.0
        return delay * self.jitter_ratio * self._rng.random()

    def _commit(self, host: str) -> None:
        now = self._clock()
        state = self._state.setdefault(host, _HostState())
        elapsed = 0.0 if state.tokens_at is None else now - state.tokens_at
        state.tokens = max(0.0, min(self.burst, state.tokens + elapsed * self.rate_per_second) - 1.0)
        state.tokens_at = now
        state.last_request_at = now

    def observe(self, host: str) -> _HostState:
        """只读访问内部状态，供测试与诊断使用。"""
        return self._state.setdefault(host, _HostState())
