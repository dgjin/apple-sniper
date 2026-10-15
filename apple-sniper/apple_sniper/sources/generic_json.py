"""通用 JSON 来源：把一个 JSON 接口 + 点路径 + 判定表达式凑成一个「有货」结论。

很多商城/比价接口都能这样接：先用 ``apple-sniper probe <目标>`` 打出真实响应，
再把路径填进 ``match.path``。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..config import ConfigError
from ..extract import MISSING, as_number, build_predicate, extract_path
from ..http import Fetcher, FetchError
from ..models import Availability, CheckResult, Target, utcnow
from .base import Source, short


class GenericJsonSource(Source):
    kind = "generic_json"

    def __init__(self, target: Target, *, clock: Callable = utcnow) -> None:
        super().__init__(target, clock=clock)
        match = target.match
        self._path = match.get("path")
        # 判定表达式在启动时就编译：写错配置应当立刻报错，而不是每次轮询静默失效。
        self._when = build_predicate(match.get("when", "exists"))
        self._price_path = match.get("price_path")
        if self._path is None and match.get("when") in (None, "exists"):
            raise ConfigError(
                f"目标 {target.id} 的 generic_json 需要 match.path（或显式给出 match.when）"
            )

    async def check(self, fetcher: Fetcher) -> CheckResult:
        try:
            response = await fetcher.get(self.target.url)
        except FetchError as exc:
            return self.failed(str(exc), detail=f"尝试 {exc.attempts} 次")
        if response.status >= 400:
            return self.failed(f"HTTP {response.status}", detail=f"{response.elapsed_ms:.0f}ms")

        try:
            data: Any = json.loads(response.text)
        except ValueError as exc:
            return self.failed(f"响应不是合法 JSON：{exc}", detail=short(response.text))

        value = extract_path(data, self._path)
        available = self._when(value)
        price = None
        if self._price_path:
            price = as_number(extract_path(data, self._price_path))

        label = self._path or "<root>"
        detail = f"{label}={short(value)}"
        if value is MISSING and self._path:
            detail += "（路径取不到值，请用 probe 核对响应字段）"
        return self.result(
            Availability.IN_STOCK if available else Availability.OUT_OF_STOCK,
            price=price,
            detail=detail,
        )
