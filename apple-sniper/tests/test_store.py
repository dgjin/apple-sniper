"""状态机：事件触发、去重、冷确、快照落盘策略。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from helpers import make_result

from apple_sniper.models import Availability, EventKind
from apple_sniper.store import Store, forget

COOLDOWN = 900.0
HEARTBEAT = 600.0
T0 = make_result().observed_at


def kinds(events) -> list[EventKind]:
    return [event.kind for event in events]


def build() -> Store:
    return Store(":memory:")


def apply(store: Store, availability=Availability.IN_STOCK, offset: float = 0.0, **kwargs):
    when = T0 + timedelta(seconds=offset)
    result = make_result(availability=availability, when=when, **kwargs)
    return store.apply(result, cooldown_seconds=COOLDOWN, heartbeat_seconds=HEARTBEAT)


class TestTransitions:
    def test_first_check_in_stock_notifies(self) -> None:
        with build() as store:
            assert kinds(apply(store, Availability.IN_STOCK)) == [EventKind.BACK_IN_STOCK]

    def test_first_check_out_of_stock_is_silent(self) -> None:
        with build() as store:
            assert apply(store, Availability.OUT_OF_STOCK) == []

    def test_no_repeat_when_state_unchanged(self) -> None:
        with build() as store:
            apply(store, Availability.IN_STOCK)
            assert apply(store, Availability.IN_STOCK, offset=30) == []
            assert apply(store, Availability.IN_STOCK, offset=60) == []

    def test_out_of_stock_after_in_stock(self) -> None:
        with build() as store:
            apply(store, Availability.IN_STOCK)
            assert kinds(apply(store, Availability.OUT_OF_STOCK, offset=30)) == [EventKind.OUT_OF_STOCK]

    def test_restock_after_out_of_stock(self) -> None:
        with build() as store:
            apply(store, Availability.OUT_OF_STOCK)
            assert kinds(apply(store, Availability.IN_STOCK, offset=30)) == [EventKind.BACK_IN_STOCK]

    def test_price_drop(self) -> None:
        with build() as store:
            apply(store, Availability.IN_STOCK, price=9999.0)
            events = apply(store, Availability.IN_STOCK, offset=30, price=9499.0)
            assert kinds(events) == [EventKind.PRICE_DROP]
            assert "¥9,999.00 → ¥9,499.00" in events[0].message

    def test_price_rise_is_ignored(self) -> None:
        with build() as store:
            apply(store, Availability.IN_STOCK, price=9499.0)
            assert apply(store, Availability.IN_STOCK, offset=30, price=9999.0) == []

    def test_message_carries_price_on_restock(self) -> None:
        with build() as store:
            events = apply(store, Availability.IN_STOCK, price=9499.0)
            assert events[0].message == "已到货，价格 ¥9,499.00"


class TestFailureHandling:
    def test_failure_keeps_last_known_availability(self) -> None:
        with build() as store:
            apply(store, Availability.IN_STOCK, price=9999.0)
            events = apply(store, Availability.UNKNOWN, offset=30, ok=False, error="HTTP 503")
            assert kinds(events) == [EventKind.ERROR]
            state = store.state("t1")
            assert state["availability"] == Availability.IN_STOCK.value
            assert state["consecutive_errors"] == 1
            assert state["stale"] is True
            assert state["last_price"] == 9999.0

    def test_recovery_does_not_fake_a_restock(self) -> None:
        """回归：失败不该把已知库存刷成 UNKNOWN，否则恢复时会被误报成「已到货」。"""
        with build() as store:
            apply(store, Availability.IN_STOCK)
            apply(store, Availability.UNKNOWN, offset=30, ok=False, error="断网")
            events = apply(store, Availability.IN_STOCK, offset=60)
            assert kinds(events) == [EventKind.RECOVERED]
            assert store.state("t1")["consecutive_errors"] == 0

    def test_repeated_failure_is_cooled_down(self) -> None:
        with build() as store:
            apply(store, Availability.UNKNOWN, ok=False, error="断网")
            assert apply(store, Availability.UNKNOWN, offset=60, ok=False, error="断网") == []
            assert kinds(apply(store, Availability.UNKNOWN, offset=COOLDOWN + 10, ok=False, error="断网")) == [
                EventKind.ERROR
            ]

    def test_error_count_accumulates_while_cooled(self) -> None:
        with build() as store:
            for offset in (0, 60, 120):
                apply(store, Availability.UNKNOWN, offset=offset, ok=False, error="断网")
            assert store.state("t1")["consecutive_errors"] == 3

    def test_restock_during_outage_still_notifies(self) -> None:
        with build() as store:
            apply(store, Availability.OUT_OF_STOCK)
            apply(store, Availability.UNKNOWN, offset=30, ok=False, error="断网")
            events = apply(store, Availability.IN_STOCK, offset=60)
            assert kinds(events) == [EventKind.RECOVERED, EventKind.BACK_IN_STOCK]


class TestSnapshots:
    def test_first_check_always_snapshots(self) -> None:
        with build() as store:
            apply(store, Availability.OUT_OF_STOCK)
            assert len(store.snapshots("t1")) == 1

    def test_unchanged_within_heartbeat_does_not_snapshot(self) -> None:
        with build() as store:
            apply(store, Availability.OUT_OF_STOCK)
            apply(store, Availability.OUT_OF_STOCK, offset=60)
            assert len(store.snapshots("t1")) == 1

    def test_heartbeat_eventually_snapshots(self) -> None:
        with build() as store:
            apply(store, Availability.OUT_OF_STOCK)
            apply(store, Availability.OUT_OF_STOCK, offset=HEARTBEAT + 1)
            assert len(store.snapshots("t1")) == 2

    def test_change_snapshots_immediately(self) -> None:
        with build() as store:
            apply(store, Availability.OUT_OF_STOCK)
            apply(store, Availability.IN_STOCK, offset=5)
            assert len(store.snapshots("t1")) == 2

    def test_failure_always_snapshots(self) -> None:
        with build() as store:
            apply(store, Availability.OUT_OF_STOCK)
            apply(store, Availability.UNKNOWN, offset=5, ok=False, error="断网")
            snapshots = store.snapshots("t1")
            assert len(snapshots) == 2
            assert snapshots[0]["ok"] == 0
            assert snapshots[0]["detail"] == "断网"


class TestQueries:
    def test_events_are_newest_first_and_filterable(self) -> None:
        with build() as store:
            apply(store, Availability.IN_STOCK)
            apply(store, Availability.OUT_OF_STOCK, offset=30)
            other = make_result("t2", Availability.IN_STOCK, T0 + timedelta(seconds=60))
            store.apply(other, cooldown_seconds=COOLDOWN, heartbeat_seconds=HEARTBEAT)

            assert [row["kind"] for row in store.recent_events(10)] == [
                "back_in_stock",
                "out_of_stock",
                "back_in_stock",
            ]
            only_t2 = store.recent_events(10, target_id="t2")
            assert len(only_t2) == 1
            assert only_t2[0]["label"] == "已到货"

    def test_all_states_reports_labels(self) -> None:
        with build() as store:
            apply(store, Availability.IN_STOCK)
            states = store.all_states()
            assert states["t1"]["availability_label"] == "有货"
            assert states["t1"]["last_event_label"] == "已到货"

    def test_since_only_moves_on_state_change(self) -> None:
        with build() as store:
            apply(store, Availability.IN_STOCK)
            first_since = store.state("t1")["since"]
            apply(store, Availability.IN_STOCK, offset=120)
            assert store.state("t1")["since"] == first_since
            apply(store, Availability.OUT_OF_STOCK, offset=180)
            assert store.state("t1")["since"] != first_since

    def test_forget_clears_everything(self) -> None:
        with build() as store:
            apply(store, Availability.IN_STOCK)
            forget(store, ["t1"])
            assert store.state("t1") is None
            assert store.recent_events() == []
            assert store.snapshots("t1") == []


def test_store_persists_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.db"
    with Store(path) as store:
        apply(store, Availability.IN_STOCK)
    with Store(path) as reopened:
        assert reopened.state("t1")["availability"] == Availability.IN_STOCK.value
        assert len(reopened.recent_events()) == 1


class TestMeta:
    """键值表：心跳这类「跨进程」的零碎状态靠它节流。"""

    def test_missing_key_is_none_then_roundtrips(self) -> None:
        with build() as store:
            assert store.get_meta("last_heartbeat_at") is None
            store.set_meta("last_heartbeat_at", "2026-10-15T12:00:00+00:00")
            assert store.get_meta("last_heartbeat_at") == "2026-10-15T12:00:00+00:00"

    def test_set_overwrites_existing_key(self) -> None:
        with build() as store:
            store.set_meta("k", "v1")
            store.set_meta("k", "v2")
            assert store.get_meta("k") == "v2"

    def test_meta_survives_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        with Store(path) as store:
            store.set_meta("last_heartbeat_at", "2026-10-15T12:00:00+00:00")
        with Store(path) as reopened:
            assert reopened.get_meta("last_heartbeat_at") == "2026-10-15T12:00:00+00:00"
