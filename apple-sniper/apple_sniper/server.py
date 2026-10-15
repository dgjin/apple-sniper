"""本地只读看板。

接口全是 GET、默认只绑 ``127.0.0.1``：这是给自己看状态的窗口，
不暴露任何写操作，也不做管理能力——要改配置就去改 YAML。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

from .config import Bundle
from .models import to_iso, utcnow
from .store import Store

STATIC_DIR = Path(__file__).parent / "static"


def _target_payload(bundle: Bundle, target) -> dict[str, Any]:
    return {
        "id": target.id,
        "name": target.name,
        "kind": target.kind,
        "url": target.url,
        "buy_url": target.buy_url,
        "action_url": target.action_url,
        "enabled": target.enabled,
        "interval_seconds": target.effective_interval(bundle.settings.interval_seconds),
    }


def create_app(bundle: Bundle) -> FastAPI:
    app = FastAPI(title="apple-sniper dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    settings = bundle.settings

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((STATIC_DIR / "dashboard.html").read_text(encoding="utf-8"))

    @app.get("/api/state")
    def state(events: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
        store = Store(settings.state_db)
        try:
            states = store.all_states()
            recent = store.recent_events(events)
        finally:
            store.close()

        targets = []
        for target in bundle.targets:
            item = _target_payload(bundle, target)
            item["state"] = states.get(target.id)
            targets.append(item)

        counts = {"targets": len(targets), "in_stock": 0, "out_of_stock": 0, "stale": 0, "unknown": 0}
        for item in targets:
            current = item["state"]
            if current is None:
                counts["unknown"] += 1
                continue
            if current["stale"]:
                counts["stale"] += 1
            counts[current["availability"]] = counts.get(current["availability"], 0) + 1

        return {
            "generated_at": to_iso(utcnow()),
            "config_dir": str(bundle.config_dir),
            "settings": {
                "interval_seconds": settings.interval_seconds,
                "jitter_ratio": settings.jitter_ratio,
                "min_host_interval_seconds": settings.min_host_interval_seconds,
                "max_requests_per_minute": settings.max_requests_per_minute,
                "renotify_cooldown_seconds": settings.renotify_cooldown_seconds,
                "state_db": settings.state_db,
                "open_url_on_hit": settings.open_url_on_hit,
                "command_on_hit": settings.command_on_hit,
                "channels": [channel.kind for channel in settings.channels],
                "notify_kinds": [kind.value for kind in settings.notify_kinds],
            },
            "counts": counts,
            "targets": targets,
            "events": recent,
        }

    @app.get("/api/history")
    def history(target: str | None = None, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        store = Store(settings.state_db)
        try:
            events = store.recent_events(limit, target_id=target)
            snapshots = store.snapshots(target, limit) if target else []
            current = store.state(target) if target else None
        finally:
            store.close()
        return {"target": target, "state": current, "events": events, "snapshots": snapshots}

    return app
