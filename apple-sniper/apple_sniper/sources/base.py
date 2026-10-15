"""来源适配器协议与共用工具。

``ok=False`` 的设计要点：抓取失败/解析失败一律返回 ``Availability.UNKNOWN``，
绝不退化成「无货」。否则一次网络抖动就会在下游触发一轮假的「已无货」事件。
"""

from __future__ import annotations

import abc
from typing import Any, Callable

from ..http import Fetcher
from ..models import Availability, CheckResult, Target, utcnow

Clock = Callable[[], Any]

MAX_DETAIL_CHARS = 160


def short(value: Any, limit: int = MAX_DETAIL_CHARS) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Source(abc.ABC):
    kind: str = ""

    def __init__(self, target: Target, *, clock: Clock = utcnow) -> None:
        self.target = target
        self._clock = clock

    @abc.abstractmethod
    async def check(self, fetcher: Fetcher) -> CheckResult:
        """抓取并判定一次；实现里不要抛异常，失败要转成 ``ok=False`` 的结果。"""

    def result(
        self,
        availability: Availability,
        *,
        price: float | None = None,
        detail: str = "",
        ok: bool = True,
        error: str | None = None,
    ) -> CheckResult:
        return CheckResult(
            target_id=self.target.id,
            availability=availability,
            observed_at=self._clock(),
            ok=ok,
            price=price,
            detail=detail,
            error=error,
        )

    def failed(self, error: str, *, detail: str = "") -> CheckResult:
        return self.result(Availability.UNKNOWN, detail=detail, ok=False, error=error)
