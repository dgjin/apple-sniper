"""HTTP 抓取层：统一 UA/超时/节流 + 退避重试。

只对**可重试**的状态码重试（429/408/425/5xx）；404 这类「页面变了」的结论
直接返回给来源层判断，不浪费重试预算，也避免把「改了路径」误诊成「网络抖动」。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from .config import Settings
from .ratelimit import HostLimiter

RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

# 服务端给出的 Retry-After 可能非常长（例如限流一小时），照睡会让监控长时间失明。
MAX_RETRY_AFTER_SECONDS = 120.0


class FetchError(Exception):
    """重试耗尽后仍未拿到响应，或拿到的是不可重试的失败。"""

    def __init__(self, message: str, *, attempts: int = 0, status: int | None = None) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.status = status


@dataclass(frozen=True)
class FetchedResponse:
    url: str
    status: int
    text: str
    headers: dict[str, str]
    elapsed_ms: float
    attempts: int


def _retry_after_seconds(raw: str | None) -> float | None:
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    if text.isdigit():
        return min(float(text), MAX_RETRY_AFTER_SECONDS)
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    delta = (moment - datetime.now(timezone.utc)).total_seconds()
    return min(max(0.0, delta), MAX_RETRY_AFTER_SECONDS)


class Fetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        limiter: HostLimiter,
        retries: int = 2,
        backoff_base_seconds: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
        on_retry: Callable[[str, int, str, float], None] | None = None,
    ) -> None:
        self._client = client
        self._limiter = limiter
        self.retries = max(0, int(retries))
        self.backoff_base_seconds = max(0.0, backoff_base_seconds)
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()
        self._on_retry = on_retry

    async def get(self, url: str) -> FetchedResponse:
        host = urlsplit(url).netloc or "local"
        attempts = self.retries + 1
        last_error = "未知错误"
        last_status: int | None = None

        for attempt in range(1, attempts + 1):
            await self._limiter.acquire(host)
            started = self._clock()
            retry_after: float | None = None
            try:
                response = await self._client.get(url)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                elapsed_ms = (self._clock() - started) * 1000.0
                if response.status_code not in RETRY_STATUS:
                    return FetchedResponse(
                        url=url,
                        status=response.status_code,
                        text=response.text,
                        headers={k.lower(): v for k, v in response.headers.items()},
                        elapsed_ms=elapsed_ms,
                        attempts=attempt,
                    )
                last_error = f"HTTP {response.status_code}"
                last_status = response.status_code
                retry_after = _retry_after_seconds(response.headers.get("retry-after"))

            if attempt >= attempts:
                break
            delay = self._backoff(attempt, retry_after)
            if self._on_retry is not None:
                self._on_retry(url, attempt, last_error, delay)
            await self._sleep(delay)

        raise FetchError(
            f"{host} 连续 {attempts} 次请求失败（最后：{last_error}）",
            attempts=attempts,
            status=last_status,
        )

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        base = retry_after if retry_after is not None else self.backoff_base_seconds * (2 ** (attempt - 1))
        return base * (1.0 + self._rng.random() * 0.25)

    async def aclose(self) -> None:
        await self._client.aclose()


def build_client(settings: Settings) -> httpx.AsyncClient:
    # trust_env=False 关掉「读系统代理」。本机 macOS 全局挂了 127.0.0.1:7890，
    # 走它会打断 api.day.app 的 TLS 握手（Connection established 后 EndOfStream），
    # 而 Apple/飞书/Bark 直连都通。需要代理时用 settings.http_proxy 显式指定。
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(settings.timeout_seconds),
        "headers": {
            "User-Agent": settings.user_agent,
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
        "follow_redirects": True,
        "trust_env": False,
    }
    if settings.http_proxy:
        kwargs["proxy"] = settings.http_proxy
    return httpx.AsyncClient(**kwargs)


def build_limiter(settings: Settings, **overrides) -> HostLimiter:
    kwargs = {
        "min_interval_seconds": settings.min_host_interval_seconds,
        "max_requests_per_minute": settings.max_requests_per_minute,
        "jitter_ratio": settings.jitter_ratio,
    }
    kwargs.update(overrides)
    return HostLimiter(**kwargs)


def build_fetcher(client: httpx.AsyncClient, settings: Settings, **overrides) -> Fetcher:
    kwargs = {
        "retries": settings.retries,
        "backoff_base_seconds": settings.backoff_base_seconds,
    }
    kwargs.update(overrides)
    return Fetcher(client, limiter=build_limiter(settings), **kwargs)
