"""测试公用工具：假时钟、可控睡眠、MockTransport 抓取器、目标构造。"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apple_sniper.http import Fetcher  # noqa: E402
from apple_sniper.models import Availability, CheckResult, Target  # noqa: E402
from apple_sniper.ratelimit import HostLimiter  # noqa: E402

UTC = timezone.utc


def run(coro):
    return asyncio.run(coro)


class FakeClock:
    """同时提供 monotonic（限速用）与 datetime（模型用），可手动推进。"""

    def __init__(self, start: datetime | None = None) -> None:
        self.now_value = start or datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
        self.mono_value = 1_700_000_000.0

    def monotonic(self) -> float:
        return self.mono_value

    def now(self) -> datetime:
        return self.now_value

    def advance(self, seconds: float) -> None:
        self.mono_value += seconds
        self.now_value = self.now_value + timedelta(seconds=seconds)


class RecordingSleep:
    def __init__(self, clock: FakeClock | None = None) -> None:
        self.calls: list[float] = []
        self._clock = clock

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self._clock is not None:
            self._clock.advance(seconds)


@contextmanager
def clean_env(**values: str) -> Iterator[None]:
    """清掉所有 APPLE_SNIPER_* 环境变量再按需注入，避免本机 shell 影响断言。"""
    saved = {key: value for key, value in os.environ.items() if key.startswith("APPLE_SNIPER_")}
    for key in saved:
        del os.environ[key]
    os.environ.update(values)
    try:
        yield
    finally:
        for key in [k for k in os.environ if k.startswith("APPLE_SNIPER_")]:
            del os.environ[key]
        os.environ.update(saved)


def make_fetcher(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    retries: int = 0,
    sleep: Any = None,
    backoff_base_seconds: float = 0.0,
    min_interval_seconds: float = 0.0,
    max_requests_per_minute: int = 6000,
    limiter: Any = None,
    on_retry: Any = None,
) -> Fetcher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    if limiter is None:
        limiter = HostLimiter(
            min_interval_seconds=min_interval_seconds,
            max_requests_per_minute=max_requests_per_minute,
            jitter_ratio=0.0,
        )
    return Fetcher(
        client,
        limiter=limiter,
        retries=retries,
        backoff_base_seconds=backoff_base_seconds,
        sleep=sleep or asyncio.sleep,
        on_retry=on_retry,
    )


def json_handler(payload: Any, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload, headers={"content-type": "application/json"})

    return handler


def make_target(ident: str = "t1", kind: str = "generic_json", **kwargs: Any) -> Target:
    defaults: dict[str, Any] = {
        "name": kwargs.pop("name", f"目标 {ident}"),
        "url": kwargs.pop("url", "https://shop.test/api/stock"),
        "buy_url": kwargs.pop("buy_url", None),
    }
    defaults.update(kwargs)
    return Target(id=ident, kind=kind, **defaults)


def make_result(
    target_id: str = "t1",
    availability: Availability = Availability.IN_STOCK,
    when: datetime | None = None,
    *,
    ok: bool = True,
    price: float | None = None,
    detail: str = "",
    error: str | None = None,
) -> CheckResult:
    return CheckResult(
        target_id=target_id,
        availability=availability,
        observed_at=when or datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC),
        ok=ok,
        price=price,
        detail=detail,
        error=error,
    )
