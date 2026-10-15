"""SQLite 状态机：把「检查结果流」压成「状态转移事件流」。

三张表 + 一张键值表：

- ``target_state``  每个目标的最新状态，也是「这个事件该不该发」的唯一依据；
- ``snapshots``     值得留痕的快照（首检 / 有变化 / 心跳）；
- ``events``        实际产生的事件（用于回看与审计）；
- ``meta``          进程间的零碎状态（如「上次心跳通知时间」），
  让每轮一个新进程的 ``check`` 也能正确节流。

两个刻意的设计决定：

1. **抓取失败不改写「已知库存」**。失败只增加 ``consecutive_errors``，
   ``availability`` 保留上一次成功判定的值。否则一次网络抖动就会把状态刷成
   UNKNOWN，等恢复时又被误判成「已到货」——假阳性比漏报更糟。
2. **事件去重靠状态转移，不靠计时器**。到货/无货本身是转移，不会重复；
   只有「抓取异常」这种持续状态用 ``renotify_cooldown_seconds`` 抑制刷屏。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .models import Availability, CheckResult, Event, EventKind, parse_iso, to_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS target_state (
    target_id          TEXT PRIMARY KEY,
    availability       TEXT NOT NULL,
    since              TEXT NOT NULL,
    last_checked       TEXT NOT NULL,
    last_price         REAL,
    last_snapshot_at   TEXT,
    last_event_kind    TEXT,
    last_event_at      TEXT,
    consecutive_errors INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id    TEXT NOT NULL,
    at           TEXT NOT NULL,
    availability TEXT NOT NULL,
    ok           INTEGER NOT NULL,
    price        REAL,
    detail       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_snapshots_target_at ON snapshots(target_id, at DESC);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id TEXT NOT NULL,
    kind      TEXT NOT NULL,
    at        TEXT NOT NULL,
    message   TEXT NOT NULL,
    price     REAL
);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at DESC);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _money(value: float) -> str:
    return f"¥{value:,.2f}"


def _event(result: CheckResult, kind: EventKind, message: str) -> Event:
    return Event(
        target_id=result.target_id,
        kind=kind,
        at=result.observed_at,
        message=message,
        price=result.price,
    )


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # --- 读 ---

    def state(self, target_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM target_state WHERE target_id = ?", (target_id,)
        ).fetchone()
        return self._decorate(row) if row else None

    def all_states(self) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM target_state").fetchall()
        return {row["target_id"]: self._decorate(row) for row in rows}

    @staticmethod
    def _decorate(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        try:
            availability = Availability(data["availability"])
        except ValueError:  # pragma: no cover - 库被人手改过
            availability = Availability.UNKNOWN
        data["availability"] = availability.value
        data["availability_label"] = availability.label
        if data.get("last_event_kind"):
            try:
                data["last_event_label"] = EventKind(data["last_event_kind"]).label
            except ValueError:  # pragma: no cover
                data["last_event_label"] = data["last_event_kind"]
        else:
            data["last_event_label"] = ""
        data["stale"] = bool(data.get("consecutive_errors"))
        return data

    def recent_events(self, limit: int = 50, *, target_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events"
        params: list[Any] = []
        if target_id:
            sql += " WHERE target_id = ?"
            params.append(target_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return [self._event_row(row) for row in self._conn.execute(sql, params).fetchall()]

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        try:
            data["label"] = EventKind(data["kind"]).label
        except ValueError:  # pragma: no cover
            data["label"] = data["kind"]
        return data

    def snapshots(self, target_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM snapshots WHERE target_id = ? ORDER BY id DESC LIMIT ?",
            (target_id, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    # --- 写 ---

    def apply(
        self,
        result: CheckResult,
        *,
        cooldown_seconds: float = 900.0,
        heartbeat_seconds: float = 600.0,
    ) -> list[Event]:
        """吸收一次检查结果，返回需要对外通知的事件（可能是空列表）。"""
        now = result.observed_at
        prev = self.state(result.target_id)

        prev_availability = Availability(prev["availability"]) if prev else Availability.UNKNOWN
        prev_price = prev["last_price"] if prev else None
        last_kind = prev["last_event_kind"] if prev else None
        last_at = parse_iso(prev["last_event_at"]) if prev else None
        last_snapshot_at = parse_iso(prev["last_snapshot_at"]) if prev else None
        prev_since = parse_iso(prev["since"]) if prev else None
        errors = int(prev["consecutive_errors"]) if prev else 0

        events: list[Event] = []

        if not result.ok:
            errors += 1
            still_cooling = last_at is not None and (now - last_at).total_seconds() < cooldown_seconds
            if last_kind != EventKind.ERROR.value or not still_cooling:
                events.append(_event(result, EventKind.ERROR, f"抓取异常：{result.error or '未知原因'}"))
            availability = prev_availability  # 保留最后已知库存
            price = prev_price
            force_snapshot = True
        else:
            errors = 0
            availability = result.availability
            price = result.price if result.price is not None else prev_price
            force_snapshot = False

            if last_kind == EventKind.ERROR.value:
                events.append(_event(result, EventKind.RECOVERED, "抓取已恢复正常"))

            if availability is Availability.IN_STOCK and prev_availability is not Availability.IN_STOCK:
                suffix = f"，价格 {_money(price)}" if price is not None else ""
                events.append(_event(result, EventKind.BACK_IN_STOCK, f"已到货{suffix}"))
            elif availability is Availability.OUT_OF_STOCK and prev_availability is Availability.IN_STOCK:
                events.append(_event(result, EventKind.OUT_OF_STOCK, "已无货"))

            if (
                result.price is not None
                and prev_price is not None
                and result.price < prev_price - 1e-9
            ):
                events.append(
                    _event(result, EventKind.PRICE_DROP, f"降价 {_money(prev_price)} → {_money(result.price)}")
                )

        changed = prev is None or prev_availability is not availability or prev_price != price
        need_snapshot = (
            force_snapshot
            or changed
            or last_snapshot_at is None
            or (now - last_snapshot_at).total_seconds() >= heartbeat_seconds
        )

        since = now if (prev is None or prev_availability is not availability) else (prev_since or now)
        emitted_kind = events[-1].kind.value if events else last_kind
        emitted_at = events[-1].at if events else last_at

        self._upsert_state(
            result.target_id,
            availability=availability,
            since=since,
            checked_at=now,
            price=price,
            snapshot_at=(now if need_snapshot else last_snapshot_at),
            last_kind=emitted_kind,
            last_at=emitted_at,
            errors=errors,
        )
        if need_snapshot:
            self._insert_snapshot(result, availability=availability, price=price)
        for event in events:
            self._insert_event(event)
        self._conn.commit()
        return events

    def _upsert_state(
        self,
        target_id: str,
        *,
        availability: Availability,
        since: Any,
        checked_at: Any,
        price: float | None,
        snapshot_at: Any,
        last_kind: str | None,
        last_at: Any,
        errors: int,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO target_state (
                target_id, availability, since, last_checked, last_price,
                last_snapshot_at, last_event_kind, last_event_at, consecutive_errors
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(target_id) DO UPDATE SET
                availability = excluded.availability,
                since = excluded.since,
                last_checked = excluded.last_checked,
                last_price = excluded.last_price,
                last_snapshot_at = excluded.last_snapshot_at,
                last_event_kind = excluded.last_event_kind,
                last_event_at = excluded.last_event_at,
                consecutive_errors = excluded.consecutive_errors
            """,
            (
                target_id,
                availability.value,
                to_iso(since),
                to_iso(checked_at),
                price,
                to_iso(snapshot_at) if snapshot_at else None,
                last_kind,
                to_iso(last_at) if last_at else None,
                int(errors),
            ),
        )

    def _insert_snapshot(self, result: CheckResult, *, availability: Availability, price: float | None) -> None:
        self._conn.execute(
            "INSERT INTO snapshots (target_id, at, availability, ok, price, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (
                result.target_id,
                to_iso(result.observed_at),
                availability.value,
                1 if result.ok else 0,
                price,
                result.detail if result.ok else (result.error or ""),
            ),
        )

    def _insert_event(self, event: Event) -> None:
        self._conn.execute(
            "INSERT INTO events (target_id, kind, at, message, price) VALUES (?, ?, ?, ?, ?)",
            (event.target_id, event.kind.value, to_iso(event.at), event.message, event.price),
        )

    # --- 键值元数据（跨进程共享的零碎状态） ---

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self._conn.commit()

    # --- 生命周期 ---

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def forget(store: Store, target_ids: Iterable[str]) -> None:
    """删除若干目标的状态与历史（用于「重新开始监控」）。"""
    ids = list(target_ids)
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    for table in ("target_state", "snapshots", "events"):
        store._conn.execute(f"DELETE FROM {table} WHERE target_id IN ({placeholders})", ids)
    store._conn.commit()
