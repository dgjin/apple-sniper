"""HTML 来源：正则命中/未命中作为「有货」依据。

两条正则的语义（常见于「页面出现『加入购物车』且不出现『暂无货』」）：
- ``present``  必须命中，否则判无货；
- ``absent``   命中即判无货（优先级高于 present）。

两者至少要给一个，否则这个来源没有任何判定能力——在启动时就报错。
"""

from __future__ import annotations

import re
from typing import Callable, Pattern

from ..config import ConfigError
from ..extract import as_number
from ..http import Fetcher, FetchError
from ..models import Availability, CheckResult, Target, utcnow
from .base import Source, short

FLAGS = re.IGNORECASE | re.DOTALL


class HtmlRegexSource(Source):
    kind = "html_regex"

    def __init__(self, target: Target, *, clock: Callable = utcnow) -> None:
        super().__init__(target, clock=clock)
        match = target.match
        present, absent = match.get("present"), match.get("absent")
        if not present and not absent:
            raise ConfigError(
                f"目标 {target.id} 的 html_regex 至少需要 match.present 或 match.absent 之一"
            )
        self._present: Pattern[str] | None = self._compile(present, target.id, "present")
        self._absent: Pattern[str] | None = self._compile(absent, target.id, "absent")
        self._price: Pattern[str] | None = self._compile(
            match.get("price_regex"), target.id, "price_regex"
        )

    @staticmethod
    def _compile(pattern: str | None, target_id: str, field: str) -> Pattern[str] | None:
        if not pattern:
            return None
        try:
            return re.compile(pattern, FLAGS)
        except re.error as exc:
            raise ConfigError(f"目标 {target_id} 的 {field} 正则非法：{exc}") from None

    async def check(self, fetcher: Fetcher) -> CheckResult:
        try:
            response = await fetcher.get(self.target.url)
        except FetchError as exc:
            return self.failed(str(exc), detail=f"尝试 {exc.attempts} 次")
        if response.status >= 400:
            return self.failed(f"HTTP {response.status}", detail=f"{response.elapsed_ms:.0f}ms")

        text = response.text
        notes: list[str] = []
        if self._absent is not None and self._absent.search(text):
            available = False
            notes.append("命中 absent 正则")
        elif self._present is not None:
            available = self._present.search(text) is not None
            notes.append("命中 present 正则" if available else "未命中 present 正则")
        else:
            available = True
            notes.append("未命中 absent 正则")

        price = None
        if self._price is not None:
            found = self._price.search(text)
            if found:
                price = as_number(found.group(1) if found.groups() else found.group(0))

        notes.append(f"响应 {len(text)} 字符")
        if self._present is None and available is False:
            notes.append("（建议补一条 present 正则，避免页面改版后长期误判）")
        return self.result(
            Availability.IN_STOCK if available else Availability.OUT_OF_STOCK,
            price=price,
            detail=f"{'；'.join(notes)}；首 80 字符：{short(text[:80])}",
        )
