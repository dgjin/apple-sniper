"""演示 / 测试用来源：从本地 JSON 文件读「库存」，完全不联网。

库存文件格式（键是目标 id，缺省条目视为无货）::

    {
      "iphone-17-pro-256-blue": {"available": true, "price": 9999, "detail": "演示"},
      "airpods-max-2026": {"available": false},
      "broken-target": {"error": "模拟抓取失败"}
    }

也支持顶层直接写 ``{"available": true}`` 作为所有目标的兜底。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ..extract import as_number, coerce_bool
from ..http import Fetcher
from ..models import Availability, CheckResult
from .base import Source


class MockSource(Source):
    kind = "mock"

    async def check(self, fetcher: Fetcher) -> CheckResult:  # noqa: ARG002 - 刻意不联网
        raw_path = self.target.match.get("inventory_file")
        if not raw_path:
            return self.failed("mock 源缺少 match.inventory_file")
        path = Path(str(raw_path))
        if not path.exists():
            return self.failed(f"库存文件不存在：{path}")

        try:
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except OSError as exc:
            return self.failed(f"读取库存文件失败：{exc}")
        try:
            data = json.loads(text)
        except ValueError as exc:
            return self.failed(f"库存文件不是合法 JSON：{exc}")

        entry = self._entry(data)
        if entry is None:
            return self.result(Availability.OUT_OF_STOCK, detail="库存文件未列出该目标")

        error = entry.get("error")
        if error:
            return self.failed(str(error))

        price = as_number(entry.get("price"))
        available = coerce_bool(entry.get("available"))
        detail = str(entry.get("detail") or ("mock 有货" if available else "mock 无货"))
        return self.result(Availability.IN_STOCK if available else Availability.OUT_OF_STOCK,
                           price=price, detail=detail)

    def _entry(self, data: Any) -> dict[str, Any] | None:
        if isinstance(data, bool):
            return {"available": data}
        if not isinstance(data, dict):
            return None
        entry = data.get(self.target.id)
        if isinstance(entry, bool):
            return {"available": entry}
        if isinstance(entry, dict):
            return entry
        if entry is None and isinstance(data.get("available"), (bool, str, int)):
            return data
        return None
