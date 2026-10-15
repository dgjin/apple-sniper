"""引擎：调度、命中动作、失败语义、通知过滤。

全部用 mock 来源 + 注入的时钟/睡眠/打开链接/命令执行，
所以这些用例不联网、不睡眠、不弹窗、不真的起进程。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from helpers import FakeClock, RecordingSleep, make_fetcher, run

from apple_sniper.config import Settings
from apple_sniper.engine import Hit, Sniper
from apple_sniper.models import Event, EventKind, Target
from apple_sniper.notify import MultiNotifier
from apple_sniper.store import Store

# --- 装置 ---


class RecordingChannel:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[tuple[EventKind, str]] = []

    async def send(self, event: Event, target: Target) -> None:
        self.sent.append((event.kind, target.id))


class ExplodingChannel:
    name = "exploding"

    async def send(self, event: Event, target: Target) -> None:
        raise RuntimeError("机器人挂了")


class ExplodingSource:
    async def check(self, fetcher: Any) -> Any:  # pragma: no cover - 立即抛错
        raise RuntimeError("来源实现有 bug")


def _no_network(request: Any) -> Any:
    raise AssertionError(f"mock 源不应发起网络请求：{request.url}")


def _write_inventory(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _mock_target(ident: str, inventory: Path, **kwargs: Any) -> Target:
    defaults: dict[str, Any] = {"name": f"目标 {ident}", "url": f"mock://{ident}"}
    defaults.update(kwargs)
    return Target(id=ident, kind="mock", match={"inventory_file": str(inventory)}, **defaults)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "interval_seconds": 0.0,  # 让所有目标在每一轮都到期，循环范围交给 max_ticks 约束
        "jitter_ratio": 0.0,
        "renotify_cooldown_seconds": 0.0,
        "heartbeat_seconds": 600.0,
    }
    base.update(overrides)
    return Settings(**base)


class Harness:
    def __init__(
        self,
        targets: tuple[Target, ...],
        *,
        settings: Settings | None = None,
        notifier: MultiNotifier | None = None,
        **engine_kwargs: Any,
    ) -> None:
        self.clock = FakeClock()
        self.sleep = RecordingSleep(self.clock)
        self.store = Store(":memory:")
        self.notifier = notifier or MultiNotifier([RecordingChannel()])
        self.fetcher = make_fetcher(_no_network)
        self.engine = Sniper(
            targets,
            settings or _settings(),
            self.store,
            self.notifier,
            self.fetcher,
            clock=self.clock.now,
            sleep=self.sleep,
            **engine_kwargs,
        )

    @property
    def channel(self) -> RecordingChannel:
        return self.notifier.channels[0]  # type: ignore[return-value]


def kinds_of(hits: list[Hit], target_id: str) -> list[EventKind]:
    for hit in hits:
        if hit.target.id == target_id:
            return [event.kind for event in hit.events]
    raise AssertionError(f"没有 {target_id} 的命中记录")


@pytest.fixture()
def inventory(tmp_path: Path) -> Path:
    path = tmp_path / "inventory.json"
    _write_inventory(path, {"t1": {"available": False}, "t2": {"available": False}})
    return path


# --- 调度与状态转移 ---


def test_run_once_reports_single_restock(inventory: Path) -> None:
    _write_inventory(inventory, {"t1": {"available": True, "price": 9999}, "t2": {"available": False}})
    harness = Harness((_mock_target("t1", inventory), _mock_target("t2", inventory)))

    stats = run(harness.engine.run(max_ticks=1))

    assert stats.ticks == 1
    assert stats.checks == 2
    assert stats.restocks == 1
    # 首检从「未知」进入「有货」算到货；从「未知」进入「无货」不算转移，不发事件。
    assert harness.channel.sent == [(EventKind.BACK_IN_STOCK, "t1")]


def test_detects_out_of_stock_then_restock(inventory: Path) -> None:
    _write_inventory(inventory, {"t1": {"available": True, "price": 9999}})
    harness = Harness((_mock_target("t1", inventory),))
    run(harness.engine.run(max_ticks=1))

    _write_inventory(inventory, {"t1": {"available": False}})
    assert kinds_of(run(harness.engine.tick()), "t1") == [EventKind.OUT_OF_STOCK]

    _write_inventory(inventory, {"t1": {"available": True, "price": 9999}})
    assert kinds_of(run(harness.engine.tick()), "t1") == [EventKind.BACK_IN_STOCK]

    assert harness.engine.stats.restocks == 2


def test_disabled_target_is_skipped(inventory: Path) -> None:
    harness = Harness(
        (_mock_target("t1", inventory), _mock_target("t2", inventory, enabled=False))
    )
    stats = run(harness.engine.run(max_ticks=1))
    assert stats.checks == 1


def test_no_enabled_targets_exits_without_ticking(inventory: Path) -> None:
    harness = Harness((_mock_target("t1", inventory, enabled=False),))
    stats = run(harness.engine.run(max_ticks=5))
    assert stats.ticks == 0
    assert stats.checks == 0


def test_run_honors_preset_stop_event(inventory: Path) -> None:
    harness = Harness((_mock_target("t1", inventory),))

    async def scenario() -> Any:
        stop = asyncio.Event()
        stop.set()
        return await harness.engine.run(stop=stop)

    stats = run(scenario())
    assert stats.ticks == 0


def test_reporter_sees_every_hit(inventory: Path) -> None:
    seen: list[Hit] = []
    harness = Harness((_mock_target("t1", inventory), _mock_target("t2", inventory)), reporter=seen.append)
    run(harness.engine.run(max_ticks=1))
    assert [hit.target.id for hit in seen] == ["t1", "t2"]


def test_interval_override_wins_over_default(inventory: Path) -> None:
    plain = _mock_target("t1", inventory)
    custom = _mock_target("t2", inventory, interval_seconds=5.0)
    harness = Harness((plain, custom), settings=_settings(interval_seconds=30.0))

    assert harness.engine._interval_for(plain) == 30.0
    assert harness.engine._interval_for(custom) == 5.0


# --- 命中动作 ---


def test_open_url_on_hit_opens_buy_url(inventory: Path) -> None:
    _write_inventory(inventory, {"t1": {"available": True, "price": 9999}})
    opened: list[str] = []

    async def opener(url: str) -> None:
        opened.append(url)

    harness = Harness(
        (_mock_target("t1", inventory, buy_url="https://example.com/buy/t1"),),
        settings=_settings(open_url_on_hit=True),
        opener=opener,
    )
    hits = run(harness.engine.tick())

    assert opened == ["https://example.com/buy/t1"]
    assert hits[0].action == "已打开 https://example.com/buy/t1"
    assert harness.engine.stats.actions == ["t1: 已打开 https://example.com/buy/t1"]


def test_open_failure_does_not_abort_tick(inventory: Path) -> None:
    _write_inventory(inventory, {"t1": {"available": True, "price": 9999}})

    async def broken_opener(url: str) -> None:
        raise OSError("没有默认浏览器")

    harness = Harness(
        (_mock_target("t1", inventory, buy_url="https://example.com/buy/t1"),),
        settings=_settings(open_url_on_hit=True),
        opener=broken_opener,
    )
    hits = run(harness.engine.tick())

    assert hits[0].restocked is True
    assert hits[0].action is None
    assert harness.engine.stats.ticks == 1


def test_command_on_hit_passes_event_through_env(inventory: Path) -> None:
    _write_inventory(inventory, {"t1": {"available": True, "price": 9499}})
    seen: list[tuple[list[str], dict[str, str]]] = []

    async def runner(argv: list[str], env: dict[str, str]) -> None:
        seen.append((argv, env))

    harness = Harness(
        (_mock_target("t1", inventory),),
        settings=_settings(command_on_hit="/bin/echo 到货了"),
        command_runner=runner,
    )
    run(harness.engine.run(max_ticks=1))

    argv, env = seen[0]
    assert argv == ["/bin/echo", "到货了"]
    assert env["APPLE_SNIPER_EVENT"] == "back_in_stock"
    assert env["APPLE_SNIPER_TARGET"] == "t1"
    assert env["APPLE_SNIPER_URL"] == "mock://t1"


def test_command_failure_is_isolated_from_notification(inventory: Path) -> None:
    _write_inventory(inventory, {"t1": {"available": True, "price": 9999}})

    async def broken_runner(argv: list[str], env: dict[str, str]) -> None:
        raise RuntimeError("脚本挂了")

    harness = Harness(
        (_mock_target("t1", inventory),),
        settings=_settings(command_on_hit="/bin/false"),
        command_runner=broken_runner,
    )
    with pytest.raises(RuntimeError, match="脚本挂了"):
        run(harness.engine.run(max_ticks=1))
    # 命中命令抛错前，通知已经发出：渠道与命中动作没有互相依赖。
    assert harness.channel.sent == [(EventKind.BACK_IN_STOCK, "t1")]


# --- 升级提醒 ---


def test_escalate_repeats_restock_notification(inventory: Path) -> None:
    _write_inventory(inventory, {"t1": {"available": True, "price": 9999}})
    harness = Harness(
        (_mock_target("t1", inventory),),
        settings=_settings(escalate_repeats=3, escalate_interval_seconds=5.0),
    )

    run(harness.engine.tick())

    assert harness.channel.sent == [(EventKind.BACK_IN_STOCK, "t1")] * 3
    # 用的是注入的睡眠，所以断言间隔等于「不真的睡」的证据。
    assert harness.sleep.calls == [5.0, 5.0]


def test_escalate_defaults_to_single_send(inventory: Path) -> None:
    _write_inventory(inventory, {"t1": {"available": True, "price": 9999}})
    harness = Harness((_mock_target("t1", inventory),))

    run(harness.engine.tick())

    assert harness.channel.sent == [(EventKind.BACK_IN_STOCK, "t1")]
    assert harness.sleep.calls == []


def test_escalate_stops_after_restock_window(inventory: Path) -> None:
    _write_inventory(inventory, {"t1": {"available": True, "price": 9999}})
    harness = Harness(
        (_mock_target("t1", inventory),),
        settings=_settings(escalate_repeats=3, escalate_interval_seconds=1.0),
    )

    run(harness.engine.tick())
    run(harness.engine.tick())  # 库存没变 → 无事件 → 不该再重复推送

    assert harness.channel.sent == [(EventKind.BACK_IN_STOCK, "t1")] * 3


# --- 通知过滤与失败隔离 ---


def test_notify_kinds_filter_keeps_event_in_store(inventory: Path) -> None:
    _write_inventory(inventory, {"t1": {"available": True, "price": 9999}})
    channel = RecordingChannel()
    notifier = MultiNotifier([channel], kinds={EventKind.OUT_OF_STOCK})
    harness = Harness((_mock_target("t1", inventory),), notifier=notifier)

    hits = run(harness.engine.tick())

    assert channel.sent == []
    assert hits[0].notified is False
    assert hits[0].restocked is True  # 事件仍然产生，只是不推送
    stored = [row["kind"] for row in harness.store.recent_events()]
    assert "back_in_stock" in stored


def test_channel_failure_does_not_break_tick(inventory: Path) -> None:
    _write_inventory(inventory, {"t1": {"available": True, "price": 9999}})
    good = RecordingChannel()
    notifier = MultiNotifier([ExplodingChannel(), good])
    harness = Harness((_mock_target("t1", inventory),), notifier=notifier)

    hits = run(harness.engine.tick())

    assert hits[0].notified is True  # 通知型渠道失败，但另一个渠道成功了
    assert good.sent == [(EventKind.BACK_IN_STOCK, "t1")]
    assert notifier.failures and "机器人挂了" in notifier.failures[0]


# --- 失败语义 ---


def test_failure_keeps_last_known_stock_then_recovers(inventory: Path) -> None:
    _write_inventory(inventory, {"t1": {"available": True, "price": 9999}})
    harness = Harness((_mock_target("t1", inventory),))
    run(harness.engine.run(max_ticks=1))

    _write_inventory(inventory, {"t1": {"error": "模拟 503"}})
    hits = run(harness.engine.tick())

    assert kinds_of(hits, "t1") == [EventKind.ERROR]
    state = harness.store.state("t1")
    assert state is not None
    # 关键：抓取失败不能把「已知有货」刷成未知，否则恢复会被误报成到货。
    assert state["availability"] == "in_stock"
    assert state["consecutive_errors"] == 1
    assert harness.engine.stats.failures == 1

    _write_inventory(inventory, {"t1": {"available": True, "price": 9999}})
    hits = run(harness.engine.tick())

    assert kinds_of(hits, "t1") == [EventKind.RECOVERED]
    assert harness.store.state("t1")["consecutive_errors"] == 0  # type: ignore[index]


def test_error_is_not_repeated_within_cooldown(inventory: Path) -> None:
    _write_inventory(inventory, {"t1": {"error": "模拟 503"}})
    harness = Harness(
        (_mock_target("t1", inventory),), settings=_settings(renotify_cooldown_seconds=3600.0)
    )

    first = run(harness.engine.tick())
    second = run(harness.engine.tick())

    assert kinds_of(first, "t1") == [EventKind.ERROR]
    assert kinds_of(second, "t1") == []


def test_source_exception_is_wrapped_as_failure(inventory: Path) -> None:
    harness = Harness((_mock_target("t1", inventory),))
    harness.engine._sources["t1"] = ExplodingSource()  # type: ignore[assignment]

    hits = run(harness.engine.tick())

    hit = hits[0]
    assert hit.result.ok is False
    assert hit.result.error is not None and "来源内部异常" in hit.result.error
    assert hit.result.error is not None and "RuntimeError" in hit.result.error
    assert harness.engine.stats.failures == 1


def test_missing_inventory_file_is_a_failure_not_a_crash(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    harness = Harness((_mock_target("t1", missing),))
    hits = run(harness.engine.tick())
    assert hits[0].result.ok is False
    assert "不存在" in (hits[0].result.error or "")


# --- 心跳通知 ---


def test_heartbeat_is_off_by_default(inventory: Path) -> None:
    harness = Harness((_mock_target("t1", inventory),))

    assert run(harness.engine.heartbeat()) is None
    assert harness.channel.sent == []


def test_heartbeat_fires_then_throttles(inventory: Path) -> None:
    harness = Harness(
        (_mock_target("t1", inventory),),
        settings=_settings(notify_heartbeat_seconds=3600.0),
    )
    run(harness.engine.tick())

    first = run(harness.engine.heartbeat())
    assert first is not None
    assert first.kind is EventKind.HEARTBEAT
    assert harness.channel.sent[-1] == (EventKind.HEARTBEAT, "heartbeat")

    # 未到间隔：不再推
    assert run(harness.engine.heartbeat()) is None
    assert harness.channel.sent.count((EventKind.HEARTBEAT, "heartbeat")) == 1

    # 越过间隔后恢复推送
    harness.clock.advance(3601)
    assert run(harness.engine.heartbeat()) is not None
    assert harness.channel.sent.count((EventKind.HEARTBEAT, "heartbeat")) == 2


def test_heartbeat_summarizes_current_states(inventory: Path) -> None:
    _write_inventory(inventory, {"t1": {"available": False}, "t2": {"available": True}})
    harness = Harness(
        (_mock_target("t1", inventory), _mock_target("t2", inventory)),
        settings=_settings(notify_heartbeat_seconds=60.0),
    )
    run(harness.engine.tick())

    event = run(harness.engine.heartbeat())

    assert event is not None
    assert "2 个目标" in event.message
    assert "有货 1" in event.message
    assert "无货 1" in event.message


def test_run_emits_one_heartbeat_across_two_passes(inventory: Path) -> None:
    harness = Harness(
        (_mock_target("t1", inventory),),
        settings=_settings(notify_heartbeat_seconds=3600.0),
    )

    run(harness.engine.run(max_ticks=2))

    # 两轮之间时钟没走，所以第二轮的心跳应被节流掉。
    assert harness.channel.sent.count((EventKind.HEARTBEAT, "heartbeat")) == 1


def test_heartbeat_throttle_state_survives_new_process(inventory: Path) -> None:
    """cron 每轮一个新进程：节流时间必须记在状态库里，而不是内存。"""
    harness = Harness(
        (_mock_target("t1", inventory),),
        settings=_settings(notify_heartbeat_seconds=3600.0),
    )
    assert run(harness.engine.heartbeat()) is not None

    fresh_channel = RecordingChannel()
    fresh = Sniper(
        (_mock_target("t1", inventory),),
        _settings(notify_heartbeat_seconds=3600.0),
        harness.store,
        MultiNotifier([fresh_channel]),
        harness.fetcher,
        clock=harness.clock.now,
    )
    assert run(fresh.heartbeat()) is None
    assert fresh_channel.sent == []
