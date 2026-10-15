"""HTTP 抓取层：重试、退避、Retry-After、节流接入。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest
from helpers import RecordingSleep, make_fetcher, run

from apple_sniper.config import Settings
from apple_sniper.http import FetchError, _retry_after_seconds, build_client

URL = "https://shop.test/api"


async def fetch(handler, *, url: str = URL, **kwargs):
    fetcher = make_fetcher(handler, **kwargs)
    try:
        return await fetcher.get(url)
    finally:
        await fetcher.aclose()


def sequence(*factories):
    """按顺序返回响应；用完之后一直重复最后一个。

    每个元素都是「返回 httpx.Response 的可调用对象」，保证每次请求拿到新对象。
    """
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        index = min(len(calls), len(factories) - 1)
        calls.append(request)
        return factories[index]()

    return handler, calls


def ok(**payload) -> object:
    return lambda: httpx.Response(200, json=payload or {"ok": True})


def status(code: int, **headers: str) -> object:
    return lambda: httpx.Response(code, headers=headers or None, text=f"status {code}")


class TestSuccess:
    def test_ok_response(self) -> None:
        handler, calls = sequence(ok(a=1))
        response = run(fetch(handler))
        assert response.status == 200
        assert response.attempts == 1
        assert len(calls) == 1
        assert response.headers["content-type"].startswith("application/json")
        assert response.elapsed_ms >= 0

    def test_client_error_is_not_retried(self) -> None:
        handler, calls = sequence(status(404))
        response = run(fetch(handler, retries=3))
        assert response.status == 404
        assert len(calls) == 1


class TestRetry:
    def test_retries_then_succeeds(self) -> None:
        handler, calls = sequence(status(503), status(503), ok())
        sleep = RecordingSleep()
        response = run(fetch(handler, retries=2, sleep=sleep))
        assert response.status == 200
        assert response.attempts == 3
        assert len(calls) == 3
        assert len(sleep.calls) == 2

    def test_exhausted_retries_raise(self) -> None:
        handler, calls = sequence(status(503), status(503))
        sleep = RecordingSleep()
        with pytest.raises(FetchError) as ctx:
            run(fetch(handler, retries=1, sleep=sleep))
        assert ctx.value.attempts == 2
        assert ctx.value.status == 503
        assert len(calls) == 2
        assert len(sleep.calls) == 1

    def test_backoff_grows_exponentially(self) -> None:
        handler, _ = sequence(*[status(500)] * 4)
        sleep = RecordingSleep()
        with pytest.raises(FetchError):
            run(fetch(handler, retries=3, backoff_base_seconds=2.0, sleep=sleep))
        # 2、4、8 秒，各自上浮最多 25% 抖动。
        assert 2.0 <= sleep.calls[0] <= 2.5
        assert 4.0 <= sleep.calls[1] <= 5.0
        assert 8.0 <= sleep.calls[2] <= 10.0

    def test_network_error_is_retried(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if len(calls) == 1:
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(200, json={"ok": True})

        sleep = RecordingSleep()
        response = run(fetch(handler, retries=1, sleep=sleep))
        assert response.status == 200
        assert response.attempts == 2
        assert len(sleep.calls) == 1

    def test_all_network_errors_raise(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timeout", request=request)

        with pytest.raises(FetchError, match="连续 2 次请求失败"):
            run(fetch(handler, retries=1))

    def test_on_retry_callback_reports(self) -> None:
        handler, _ = sequence(status(429), ok())
        seen: list[tuple[str, int, str]] = []
        fetcher = make_fetcher(
            handler,
            retries=1,
            sleep=RecordingSleep(),
            on_retry=lambda url, attempt, error, delay: seen.append((url, attempt, error)),
        )

        async def scenario():
            try:
                return await fetcher.get(URL)
            finally:
                await fetcher.aclose()

        run(scenario())
        assert seen == [(URL, 1, "HTTP 429")]


class TestRetryAfter:
    def test_seconds_header_respected(self) -> None:
        handler, _ = sequence(status(429, **{"retry-after": "3"}), ok())
        sleep = RecordingSleep()
        run(fetch(handler, retries=1, sleep=sleep))
        assert 3.0 <= sleep.calls[0] <= 3.75

    def test_zero_retry_after_means_wait_nothing(self) -> None:
        handler, _ = sequence(status(429, **{"retry-after": "0"}), ok())
        sleep = RecordingSleep()
        run(fetch(handler, retries=1, backoff_base_seconds=30.0, sleep=sleep))
        assert sleep.calls[0] == 0.0

    def test_huge_retry_after_is_capped(self) -> None:
        handler, _ = sequence(status(429, **{"retry-after": "9999"}), ok())
        sleep = RecordingSleep()
        run(fetch(handler, retries=1, sleep=sleep))
        assert sleep.calls[0] <= 150.0

    def test_http_date_header_supported(self) -> None:
        moment = datetime.now(timezone.utc) + timedelta(seconds=2)
        handler, _ = sequence(
            status(429, **{"retry-after": format_datetime(moment, usegmt=True)}), ok()
        )
        sleep = RecordingSleep()
        run(fetch(handler, retries=1, sleep=sleep))
        assert 0.0 < sleep.calls[0] <= 2.6

    def test_parser_handles_junk(self) -> None:
        assert _retry_after_seconds(None) is None
        assert _retry_after_seconds("  ") is None
        assert _retry_after_seconds("soon") is None


class TestLimiterWiring:
    def test_limiter_is_called_per_host(self) -> None:
        class RecordingLimiter:
            def __init__(self) -> None:
                self.hosts: list[str] = []

            async def acquire(self, host: str) -> float:
                self.hosts.append(host)
                return 0.0

        limiter = RecordingLimiter()
        handler, _ = sequence(ok())

        async def scenario() -> None:
            fetcher = make_fetcher(handler, limiter=limiter)
            try:
                await fetcher.get("https://a.test/x")
                await fetcher.get("https://b.test/y")
            finally:
                await fetcher.aclose()

        run(scenario())
        assert limiter.hosts == ["a.test", "b.test"]


class TestBuildClient:
    """出口策略：默认直连（忽略系统代理），配了 http_proxy 才走代理。"""

    def test_default_is_direct(self) -> None:
        client = build_client(Settings())
        try:
            assert client._trust_env is False
            assert not client._mounts
        finally:
            run(client.aclose())

    def test_http_proxy_mounts_transport(self) -> None:
        client = build_client(Settings(http_proxy="http://127.0.0.1:7890"))
        try:
            assert client._trust_env is False
            assert client._mounts
        finally:
            run(client.aclose())
