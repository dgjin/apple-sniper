"""Apple 官网「到店取货」可用性来源。

字段名会随 Apple 前端改版变化，因此**路径全部走配置**：代码里只保留一组候选
路径作缺省值，真值以 ``apple-sniper probe <目标>`` 打印的响应为准。

典型配置（``match``）：::

    paths:                      # 依次尝试，命中第一个存在的
      - content.pickupMessage.stores
      - body.content.pickupMessage.stores
    store_availability_path: partsAvailability   # 门店节点里的可用性字段
    store_availability_key: MTVJ3CH/A            # 上者若是按部件号索引的字典，用这个键取值
    store_name_path: storeName
    when: truthy                                 # 没配 store_availability_path 时的兜底判定
"""

from __future__ import annotations

import json

from ..config import ConfigError
from ..extract import MISSING, as_number, build_predicate, coerce_bool, extract_path
from ..http import Fetcher, FetchError
from ..models import Availability, CheckResult, utcnow
from .base import Source, short

DEFAULT_PATHS = (
    "content.pickupMessage.stores",
    "body.content.pickupMessage.stores",
    "body.stores",
    "stores",
)


def _join(names: list[str], limit: int) -> str:
    if not names:
        return ""
    head = names[:limit]
    suffix = f" 等 {len(names)} 家" if len(names) > limit else ""
    return "、".join(head) + suffix


class AppleFulfillmentSource(Source):
    kind = "apple_fulfillment"

    def __init__(self, target, *, clock=utcnow) -> None:
        super().__init__(target, clock=clock)
        match = target.match
        paths = match.get("paths")
        if paths is not None and not isinstance(paths, (list, tuple)):
            raise ConfigError(f"目标 {target.id} 的 match.paths 需要列表，实际是 {paths!r}")
        # 未配置走缺省候选路径；显式配成空列表属于写错配置，要立刻报错而不是静默回落。
        self._paths: tuple[str, ...] = DEFAULT_PATHS if paths is None else tuple(str(p) for p in paths)
        if not self._paths:
            raise ConfigError(f"目标 {target.id} 的 match.paths 不能为空列表")
        self._when = build_predicate(match.get("when", "truthy"))
        self._store_availability_path = match.get("store_availability_path")
        self._store_availability_key = match.get("store_availability_key")
        self._store_name_path = match.get("store_name_path", "storeName")
        self._price_path = match.get("price_path")
        self._max_stores = int(match.get("max_stores_in_detail", 5))

    async def check(self, fetcher: Fetcher) -> CheckResult:
        try:
            response = await fetcher.get(self.target.url)
        except FetchError as exc:
            return self.failed(str(exc), detail=f"尝试 {exc.attempts} 次")
        if response.status >= 400:
            return self.failed(f"HTTP {response.status}", detail=f"{response.elapsed_ms:.0f}ms")

        try:
            data = json.loads(response.text)
        except ValueError as exc:
            return self.failed(f"响应不是合法 JSON：{exc}", detail=short(response.text))

        path, value = self._first_hit(data)
        if value is MISSING:
            tried = "、".join(self._paths)
            return self.failed(
                f"响应里找不到任何候选路径（已试：{tried}）；用 probe 核对实际字段名",
                detail=short(response.text),
            )

        notes: list[str] = []
        if isinstance(value, (list, tuple)) and self._store_availability_path:
            available, names = self._scan_stores(value)
            notes.append(f"{path}（{len(value)} 家门店）有货：{_join(names, self._max_stores) or '无'}")
        else:
            if isinstance(value, (list, tuple)):
                notes.append(f"{path}=非空数组，未配置 store_availability_path，只按整体判定")
            else:
                notes.append(f"{path}={short(value)}")
            available = self._when(value)

        price = None
        if self._price_path:
            price = as_number(extract_path(data, self._price_path))

        return self.result(
            Availability.IN_STOCK if available else Availability.OUT_OF_STOCK,
            price=price,
            detail="；".join(notes),
        )

    def _first_hit(self, data: object) -> tuple[str, object]:
        for path in self._paths:
            value = extract_path(data, path)
            if value is not MISSING:
                return path, value
        return self._paths[0], MISSING

    def _scan_stores(self, stores) -> tuple[bool, list[str]]:
        available = False
        names: list[str] = []
        for store in stores:
            node = extract_path(store, self._store_availability_path)
            if self._store_availability_key and self._store_availability_path:
                node = extract_path(node, self._store_availability_key)
            if not coerce_bool(node):
                continue
            available = True
            name = extract_path(store, self._store_name_path)
            if name is not MISSING and name is not None:
                names.append(str(name))
        return available, names
