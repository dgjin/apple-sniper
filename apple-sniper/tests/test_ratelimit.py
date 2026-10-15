"""请求节流：最小间隔、令牌桶、抖动方向、按主机隔离。"""

from __future__ import annotations

import random

from helpers import FakeClock, RecordingSleep, run

from apple_sniper.ratelimit import HostLimiter


def build(**kwargs):
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    defaults = {
        "min_interval_seconds": 0.0,
        "max_requests_per_minute": 6000,
        "jitter_ratio": 0.0,
        "clock": clock.monotonic,
        "sleep": sleep,
    }
    defaults.update(kwargs)
    return HostLimiter(**defaults), clock, sleep


class TestPlan:
    def test_first_request_has_no_delay(self) -> None:
        limiter, _, _ = build(min_interval_seconds=5)
        assert limiter.plan("a.test") == 0.0

    def test_min_interval_applies_after_commit(self) -> None:
        limiter, clock, _ = build(min_interval_seconds=5)
        limiter._commit("a.test")
        assert limiter.plan("a.test") == 5.0
        clock.advance(2)
        assert limiter.plan("a.test") == 3.0
        clock.advance(5)
        assert limiter.plan("a.test") == 0.0

    def test_token_bucket_paces_even_without_min_interval(self) -> None:
        # 60 次/分 => 平均每 1 秒一次；突发额度为 1，所以紧接着的第二次要等 1 秒。
        limiter, _, _ = build(min_interval_seconds=0, max_requests_per_minute=60)
        limiter._commit("a.test")
        assert limiter.plan("a.test") == 1.0

    def test_rate_limit_is_never_tighter_than_min_interval(self) -> None:
        limiter, _, _ = build(min_interval_seconds=8, max_requests_per_minute=600)
        limiter._commit("a.test")
        assert limiter.plan("a.test") == 8.0

    def test_hosts_are_independent(self) -> None:
        limiter, _, _ = build(min_interval_seconds=5)
        limiter._commit("a.test")
        assert limiter.plan("b.test") == 0.0
        assert limiter.plan("a.test") == 5.0

    def test_jitter_only_adds_delay(self) -> None:
        limiter, _, _ = build(min_interval_seconds=10, jitter_ratio=0.3, rng=random.Random(7))
        limiter._commit("a.test")
        delay = limiter.plan("a.test")
        assert 10.0 <= delay <= 13.0

    def test_jitter_does_not_touch_zero_delay(self) -> None:
        limiter, _, _ = build(min_interval_seconds=1, jitter_ratio=0.5, rng=random.Random(7))
        assert limiter.plan("fresh.test") == 0.0

    def test_zero_rate_is_rejected(self) -> None:
        try:
            HostLimiter(max_requests_per_minute=0)
        except ValueError as exc:
            assert "正数" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("应当拒绝 0 速率")


class TestAcquire:
    def test_second_request_waits_for_min_interval(self) -> None:
        limiter, clock, sleep = build(min_interval_seconds=2)

        async def scenario() -> list[float]:
            waited = [await limiter.acquire("a.test"), await limiter.acquire("a.test")]
            return waited

        waited = run(scenario())
        assert waited[0] == 0.0
        assert waited[1] == 2.0
        assert sleep.calls == [2.0]
        # 提交发生在 t0+2：最小间隔继续生效，下一次仍要等到 t0+4。
        assert limiter.plan("a.test") == 2.0

    def test_acquire_loops_until_delay_is_gone(self) -> None:
        # 令牌桶与最小间隔叠加时，一次 sleep 之后仍可能不够，必须能收敛。
        limiter, _, sleep = build(min_interval_seconds=1, max_requests_per_minute=60)

        async def scenario() -> float:
            await limiter.acquire("a.test")
            return await limiter.acquire("a.test")

        waited = run(scenario())
        assert waited == 1.0
        assert sleep.calls == [1.0]
