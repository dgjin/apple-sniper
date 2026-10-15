"""监控引擎：按目标调度 → 抓取判定 → 状态机产出事件 → 通知 → 命中动作。

依赖全部可注入（时钟、睡眠、随机数、打开链接、命令执行），
所以端到端逻辑能在**不联网、不睡眠、不弹窗**的前提下被单测覆盖。
"""

from __future__ import annotations

import asyncio
import logging
import random
import sys
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

from .config import Settings
from .http import Fetcher
from .models import Availability, CheckResult, Event, EventKind, Target, parse_iso, to_iso, utcnow
from .notify import MultiNotifier
from .notify.channels import CommandChannel
from .sources import create_source
from .sources.base import Source
from .store import Store

logger = logging.getLogger(__name__)
Opener = Callable[[str], Awaitable[None]]

# 心跳用的是「汇总」通知，没有具体商品页可跳，所以借一个合成目标承载标题/文案。
HEARTBEAT_KEY = "last_heartbeat_at"
HEARTBEAT_TARGET = Target(id="heartbeat", name="apple-sniper", kind="mock", url="")


async def _open_in_browser(url: str) -> None:
    argv = ["open", url] if sys.platform == "darwin" else ["xdg-open", url]
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await process.wait()


@dataclass
class Hit:
    target: Target
    result: CheckResult
    events: tuple[Event, ...] = ()
    notified: bool = False
    action: str | None = None

    @property
    def restocked(self) -> bool:
        return any(event.kind is EventKind.BACK_IN_STOCK for event in self.events)


@dataclass
class EngineStats:
    ticks: int = 0
    checks: int = 0
    failures: int = 0
    events: int = 0
    restocks: int = 0
    actions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticks": self.ticks,
            "checks": self.checks,
            "failures": self.failures,
            "events": self.events,
            "restocks": self.restocks,
        }


class Sniper:
    def __init__(
        self,
        targets: Sequence[Target],
        settings: Settings,
        store: Store,
        notifier: MultiNotifier,
        fetcher: Fetcher,
        *,
        clock: Callable[[], Any] = utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
        opener: Opener | None = None,
        command_runner: Callable[[list[str], dict[str, str]], Awaitable[None]] | None = None,
        reporter: Callable[[Hit], None] | None = None,
    ) -> None:
        self.targets = tuple(targets)
        self.settings = settings
        self.store = store
        self.notifier = notifier
        self.fetcher = fetcher
        self.stats = EngineStats()
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._opener = opener or _open_in_browser
        self._reporter = reporter
        # 构造期就建好来源与命中命令：配置/正则/判定表达式写错应当在启动时报错。
        self._sources: dict[str, Source] = {t.id: create_source(t, clock=clock) for t in self.targets}
        self._hit_command = (
            CommandChannel(settings.command_on_hit, runner=command_runner)
            if settings.command_on_hit
            else None
        )

    # --- 单次检查 ---

    async def check_one(self, target: Target) -> Hit:
        source = self._sources[target.id]
        try:
            result = await source.check(self.fetcher)
        except Exception as exc:  # 来源实现的 bug 不能把整轮监控带走
            logger.exception("来源 %s 抛出异常", target.kind)
            result = CheckResult(
                target_id=target.id,
                availability=Availability.UNKNOWN,
                observed_at=self._clock(),
                ok=False,
                error=f"来源内部异常：{type(exc).__name__}: {exc}",
            )
        self.stats.checks += 1
        if not result.ok:
            self.stats.failures += 1

        events = self.store.apply(
            result,
            cooldown_seconds=self.settings.renotify_cooldown_seconds,
            heartbeat_seconds=self.settings.heartbeat_seconds,
        )
        hit = Hit(target=target, result=result, events=tuple(events))
        self.stats.events += len(events)

        for event in events:
            delivered = await self.notifier.send(event, target)
            hit.notified = hit.notified or delivered

        if hit.restocked:
            self.stats.restocks += 1
            # 先做命中动作（打开购买页是要抢时间的），再重复提醒去「追」人。
            hit.action = await self.apply_hit_actions(target, hit.events)
            if hit.action:
                self.stats.actions.append(f"{target.id}: {hit.action}")
            await self._escalate(target, hit.events)
        return hit

    async def tick(self) -> list[Hit]:
        """跑一轮：所有启用的目标各查一次。"""
        self.stats.ticks += 1
        hits: list[Hit] = []
        for target in self.targets:
            if not target.enabled:
                continue
            hit = await self.check_one(target)
            hits.append(hit)
            self._report(hit)
        return hits

    # --- 循环 ---

    async def run(self, *, max_ticks: int | None = None, stop: Any = None) -> EngineStats:
        """按各自间隔轮询直到 ``max_ticks`` 或 ``stop``。

        每个目标独立计时：短间隔的目标不会被长间隔的目标拖慢。
        """
        enabled = [t for t in self.targets if t.enabled]
        if not enabled:
            logger.warning("没有启用的目标，直接退出")
            return self.stats

        loop = asyncio.get_running_loop()
        next_due = {t.id: loop.time() for t in enabled}

        while not self._stopping(stop, max_ticks):
            now = loop.time()
            due = [t for t in enabled if next_due[t.id] <= now]
            if not due:
                await self._pause(min(next_due[t.id] for t in enabled) - now, stop)
                continue
            self.stats.ticks += 1
            for target in due:
                if stop is not None and stop.is_set():
                    break
                hit = await self.check_one(target)
                self._report(hit)
                next_due[target.id] = loop.time() + self._interval_for(target)
            await self.heartbeat()
        return self.stats

    def _interval_for(self, target: Target) -> float:
        base = target.effective_interval(self.settings.interval_seconds)
        if self.settings.jitter_ratio <= 0:
            return base
        # 目标之间错开相位，避免所有目标在同一秒齐射（对站点更友好）。
        return base * (1.0 + self._rng.uniform(0, self.settings.jitter_ratio))

    def _stopping(self, stop: Any, max_ticks: int | None) -> bool:
        if stop is not None and stop.is_set():
            return True
        return max_ticks is not None and self.stats.ticks >= max_ticks

    async def _pause(self, seconds: float, stop: Any) -> None:
        if seconds <= 0:
            return
        if stop is None:
            await self._sleep(seconds)
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # --- 命中动作 ---

    async def _escalate(self, target: Target, events: Sequence[Event]) -> None:
        """到货后按间隔重复推送，直到 ``escalate_repeats`` 用尽。

        这是「重复提醒」而不是「反复探测」：只是把同一条到货事件多推几次，
        不改请求频率。``escalate_repeats`` 含第一次，所以默认 1 时不额外推送。
        """
        extra = max(0, int(self.settings.escalate_repeats) - 1)
        if extra == 0 or not events:
            return
        interval = max(0.0, self.settings.escalate_interval_seconds)
        for _ in range(extra):
            await self._sleep(interval)
            for event in events:
                await self.notifier.send(event, target)

    async def apply_hit_actions(self, target: Target, events: Sequence[Event]) -> str | None:
        """执行命中动作（打开购买页 / command_on_hit），返回人可读的执行说明。

        公开是因为 `drill` 子命令要复用同一条路径来演练，而不是另写一份。
        """
        done: list[str] = []
        if self.settings.open_url_on_hit:
            try:
                await self._opener(target.action_url)
                done.append(f"已打开 {target.action_url}")
            except Exception as exc:  # 打不开浏览器不该影响后续动作与通知
                logger.warning("打开链接失败：%s", exc)
        if self._hit_command is not None:
            event = events[0]
            await self._hit_command.send(event, target)
            done.append(f"已执行命中命令：{self.settings.command_on_hit}")
        return "；".join(done) or None

    # --- 心跳 ---

    async def heartbeat(self) -> Event | None:
        """到点就推一条「监控仍在运行」的汇总；未到点返回 ``None``。

        节流时间记在状态库的 ``meta`` 表里（而不是内存），所以每轮一个新进程的
        ``check`` 也能正确按间隔推送。间隔设 0 表示关闭。
        """
        interval = self.settings.notify_heartbeat_seconds
        if interval <= 0:
            return None
        now = self._clock()
        last = parse_iso(self.store.get_meta(HEARTBEAT_KEY))
        if last is not None and (now - last).total_seconds() < interval:
            return None

        event = Event(
            target_id=HEARTBEAT_TARGET.id,
            kind=EventKind.HEARTBEAT,
            at=now,
            message=self._heartbeat_message(),
        )
        self.store.set_meta(HEARTBEAT_KEY, to_iso(now))
        await self.notifier.send(event, HEARTBEAT_TARGET)
        logger.info("心跳通知：%s", event.message)
        return event

    def _heartbeat_message(self) -> str:
        enabled = [target for target in self.targets if target.enabled]
        states = self.store.all_states()
        counts: dict[str, int] = {}
        stale = 0
        for target in enabled:
            state = states.get(target.id)
            if state is None:
                counts["未检查"] = counts.get("未检查", 0) + 1
                continue
            label = state["availability_label"]
            counts[label] = counts.get(label, 0) + 1
            if state["stale"]:
                stale += 1
        summary = "、".join(f"{label} {count}" for label, count in counts.items()) or "暂无记录"
        trouble = f"，其中 {stale} 个最近抓取失败" if stale else ""
        return f"监控运行中：{len(enabled)} 个目标（{summary}{trouble}）"

    def _report(self, hit: Hit) -> None:
        if self._reporter is not None:
            self._reporter(hit)
            return
        level = logger.info if hit.result.ok else logger.warning
        level(
            "%s %s（%s）%s",
            hit.target.id,
            hit.result.availability.label,
            hit.result.detail or "无详情",
            "；事件：" + "、".join(e.kind.label for e in hit.events) if hit.events else "",
        )
