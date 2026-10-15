"""领域模型：监控目标、单次检查结果、状态事件。

全部时间戳统一为 **带时区的 UTC**，落库前转 ISO 字符串，
避免本地时区/夏令时把「冷却期」和「心跳间隔」算歪。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


class Availability(str, Enum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {self.IN_STOCK: "有货", self.OUT_OF_STOCK: "无货", self.UNKNOWN: "未知"}[self]


class EventKind(str, Enum):
    BACK_IN_STOCK = "back_in_stock"
    OUT_OF_STOCK = "out_of_stock"
    PRICE_DROP = "price_drop"
    ERROR = "error"
    RECOVERED = "recovered"
    HEARTBEAT = "heartbeat"

    @property
    def label(self) -> str:
        return {
            self.BACK_IN_STOCK: "已到货",
            self.OUT_OF_STOCK: "已无货",
            self.PRICE_DROP: "降价",
            self.ERROR: "抓取异常",
            self.RECOVERED: "恢复正常",
            self.HEARTBEAT: "心跳",
        }[self]


@dataclass(frozen=True)
class Target:
    """一个被监控的商品页面。"""

    id: str
    name: str
    kind: str
    url: str
    enabled: bool = True
    interval_seconds: float | None = None
    buy_url: str | None = None
    notify: tuple[str, ...] = ()
    match: dict[str, Any] = field(default_factory=dict)

    def effective_interval(self, default: float) -> float:
        return self.interval_seconds if self.interval_seconds else default

    @property
    def action_url(self) -> str:
        return self.buy_url or self.url


@dataclass(frozen=True)
class CheckResult:
    """一次检查的结论。`ok=False` 表示**没拿到可信结论**（网络/解析失败），
    与「确认无货」严格区分——否则一次抖动就会误报成「已无货」。"""

    target_id: str
    availability: Availability
    observed_at: datetime
    ok: bool = True
    price: float | None = None
    detail: str = ""
    error: str | None = None


@dataclass(frozen=True)
class Event:
    """状态转移事件，也是通知的最小载荷。"""

    target_id: str
    kind: EventKind
    at: datetime
    message: str
    price: float | None = None
    url: str | None = None
