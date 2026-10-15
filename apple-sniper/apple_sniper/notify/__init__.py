"""通知 fan-out：一个事件发给所有渠道，失败被隔离并记录。"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Protocol

from ..config import Settings
from ..models import Event, EventKind, Target
from .channels import build_channel, format_event, title_for

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    name: str

    async def send(self, event: Event, target: Target) -> None: ...


class MultiNotifier:
    """按 ``notify_kinds`` 过滤后再 fan-out；单渠道异常不影响其它渠道。"""

    def __init__(self, channels: list[Any], *, kinds: set[EventKind] | None = None) -> None:
        self.channels = channels
        self.kinds = kinds
        self.failures: list[str] = []

    def wants(self, kind: EventKind) -> bool:
        return self.kinds is None or kind in self.kinds

    async def send(self, event: Event, target: Target) -> bool:
        if not self.wants(event.kind):
            return False
        delivered = False
        for channel in self.channels:
            try:
                await channel.send(event, target)
                delivered = True
            except Exception as exc:  # 渠道是外部系统，任何异常都不能掀翻监控循环
                note = f"{getattr(channel, 'name', channel)} 渠道失败：{exc}"
                self.failures.append(note)
                logger.warning("通知发送失败：%s", note)
        return delivered

    async def aclose(self) -> None:
        for channel in self.channels:
            closer = getattr(channel, "aclose", None)
            if closer is None:
                continue
            try:
                await closer()
            except Exception as exc:  # pragma: no cover - 关闭失败无需影响退出
                logger.debug("关闭渠道失败：%s", exc)


def build_notifier(
    settings: Settings,
    *,
    writer: Callable[[str], Any] = print,
    macos_runner: Callable[[list[str]], Awaitable[None]] | None = None,
    command_runner: Callable[[list[str], dict[str, str]], Awaitable[None]] | None = None,
    platform: str | None = None,
    webhook_client: Any = None,
    on_error: Callable[[str], None] | None = None,
) -> MultiNotifier:
    channels = [
        build_channel(
            config.kind,
            config.options,
            writer=writer,
            macos_runner=macos_runner,
            command_runner=command_runner,
            platform=platform,
            webhook_client=webhook_client,
            on_error=on_error,
        )
        for config in settings.channels
    ]
    return MultiNotifier(channels, kinds=set(settings.notify_kinds))


__all__ = ["MultiNotifier", "Notifier", "build_notifier", "format_event", "title_for"]
