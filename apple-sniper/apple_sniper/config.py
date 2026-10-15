"""配置加载与校验。

两份 YAML：

- ``settings.yaml``   运行参数（轮询间隔、限速、状态库、通知渠道、命中动作）
- ``targets.yaml``    监控目标（URL、来源类型、判定方式、单独覆盖的间隔）

运行参数都可用 ``APPLE_SNIPER_*`` 环境变量覆盖，方便临时调试而不用改文件。
所有校验失败都抛 :class:`ConfigError`，信息里必须带**文件名 + 字段名**。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from .models import EventKind, Target

ENV_PREFIX = "APPLE_SNIPER_"
DEFAULT_CONFIG_DIR = "config"

# 来源类型 / 通知渠道的合法取值：写错配置要在**启动时**就报错，
# 而不是等到某一次轮询才静默失效。
SOURCE_KINDS = frozenset({"mock", "generic_json", "html_regex", "apple_fulfillment"})
CHANNEL_KINDS = frozenset({"console", "macos", "webhook", "command"})

DEFAULT_EVENT_KINDS = (
    EventKind.BACK_IN_STOCK,
    EventKind.OUT_OF_STOCK,
    EventKind.PRICE_DROP,
    EventKind.ERROR,
)


class ConfigError(Exception):
    """配置不可用。"""


@dataclass(frozen=True)
class ChannelConfig:
    kind: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Settings:
    """运行参数。默认值刻意偏「保守」：低频、串行、带抖动。"""

    interval_seconds: float = 30.0
    jitter_ratio: float = 0.3
    min_host_interval_seconds: float = 3.0
    max_requests_per_minute: int = 20
    timeout_seconds: float = 10.0
    retries: int = 2
    backoff_base_seconds: float = 1.0
    # 出口代理。缺省 None = **直连**，刻意忽略系统代理：本机 macOS 的系统代理
    # 会把 api.day.app 的 TLS 握手中断（走代理必失败），而直连三个目标站点都正常。
    # 需要走代理时显式填 "http://127.0.0.1:7890" 之类。
    http_proxy: str | None = None
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 apple-sniper/0.1 "
        "(personal availability monitor; contact: local user)"
    )
    state_db: str = "data/apple_sniper.db"
    heartbeat_seconds: float = 600.0
    renotify_cooldown_seconds: float = 900.0
    # 命中「已到货」后的升级提醒：把通知重复推送 N 次、每次间隔若干秒，
    # 免得人在开会/锁屏时错过。默认 1 = 只推一次（保持原行为）。
    escalate_repeats: int = 1
    escalate_interval_seconds: float = 5.0
    max_concurrency: int = 1
    status_every_ticks: int = 0
    # 心跳通知间隔（秒）。0 = 关。到点推一条「监控仍在运行」的汇总，
    # 用来区分「还没到货」和「监控悄悄挂了」——cron 每轮一个新进程，故节流记在状态库里。
    notify_heartbeat_seconds: float = 0.0
    open_url_on_hit: bool = False
    command_on_hit: str | None = None
    notify_kinds: tuple[EventKind, ...] = DEFAULT_EVENT_KINDS
    channels: tuple[ChannelConfig, ...] = ()


@dataclass(frozen=True)
class Bundle:
    settings: Settings
    targets: tuple[Target, ...]
    config_dir: Path


def resolve_config_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    raw = explicit or os.environ.get(ENV_PREFIX + "CONFIG_DIR") or DEFAULT_CONFIG_DIR
    return Path(raw).expanduser().resolve()


def _read_yaml(path: Path) -> Any:
    if not path.exists():
        raise ConfigError(f"找不到配置文件 {path}")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - 信息透传
        raise ConfigError(f"{path} YAML 解析失败：{exc}") from exc


def _env(name: str) -> str | None:
    value = os.environ.get(ENV_PREFIX + name)
    return value if value not in (None, "") else None


def _number(raw: dict[str, Any], key: str, default: float, where: str) -> float:
    value = raw.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where} 字段 {key} 需要数字，实际是 {value!r}") from None


def _integer(raw: dict[str, Any], key: str, default: int, where: str) -> int:
    value = raw.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where} 字段 {key} 需要整数，实际是 {value!r}") from None


def _boolean(raw: dict[str, Any], key: str, default: bool, where: str) -> bool:
    value = raw.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "on", "1"}:
            return True
        if lowered in {"false", "no", "off", "0"}:
            return False
    raise ConfigError(f"{where} 字段 {key} 需要布尔值，实际是 {value!r}")


def _resolve_path(value: str, base: Path) -> str:
    """相对路径按**项目根**（配置目录的父级）解析，避免受当前工作目录影响。"""
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else (base / path))


def _parse_event_kinds(raw: dict[str, Any], where: str) -> tuple[EventKind, ...]:
    values = raw.get("notify_kinds")
    if values is None:
        return DEFAULT_EVENT_KINDS
    if not isinstance(values, (list, tuple)):
        raise ConfigError(f"{where} 的 notify_kinds 需要列表，实际是 {values!r}")
    kinds: list[EventKind] = []
    for item in values:
        try:
            kinds.append(EventKind(str(item)))
        except ValueError:
            allowed = ", ".join(k.value for k in EventKind)
            raise ConfigError(
                f"{where} 的 notify_kinds 含未知事件 {item!r}（可选：{allowed}）"
            ) from None
    return tuple(kinds)


def _parse_channels(raw: dict[str, Any], where: str) -> tuple[ChannelConfig, ...]:
    items = raw.get("channels")
    if items is None:
        raise ConfigError(f"{where} 缺少 channels：至少要有 console 渠道，否则命中无人知晓")
    if not isinstance(items, (list, tuple)) or not items:
        raise ConfigError(f"{where} 的 channels 需要非空列表，实际是 {items!r}")

    channels: list[ChannelConfig] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ConfigError(f"{where} 的 channels[{index}] 需要字典，实际是 {item!r}")
        kind = item.get("kind")
        if kind not in CHANNEL_KINDS:
            allowed = ", ".join(sorted(CHANNEL_KINDS))
            raise ConfigError(
                f"{where} 的 channels[{index}].kind={kind!r} 不支持（可选：{allowed}）"
            )
        options = {k: v for k, v in item.items() if k != "kind"}
        channels.append(ChannelConfig(kind=str(kind), options=options))
    return tuple(channels)


def load_settings(path: str | os.PathLike[str] | None = None, *, base_dir: Path | None = None) -> Settings:
    base = base_dir or Path.cwd()
    target_path = Path(path) if path else base / "settings.yaml"
    raw = _read_yaml(target_path)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{target_path} 顶层需要字典，实际是 {type(raw).__name__}")
    where = target_path.name

    settings = Settings(
        interval_seconds=_number(raw, "interval_seconds", 30.0, where),
        jitter_ratio=_number(raw, "jitter_ratio", 0.3, where),
        min_host_interval_seconds=_number(raw, "min_host_interval_seconds", 3.0, where),
        max_requests_per_minute=_integer(raw, "max_requests_per_minute", 20, where),
        timeout_seconds=_number(raw, "timeout_seconds", 10.0, where),
        retries=_integer(raw, "retries", 2, where),
        backoff_base_seconds=_number(raw, "backoff_base_seconds", 1.0, where),
        http_proxy=(str(raw["http_proxy"]) if raw.get("http_proxy") else None),
        user_agent=str(raw.get("user_agent") or Settings.user_agent),
        state_db=str(raw.get("state_db") or Settings.state_db),
        heartbeat_seconds=_number(raw, "heartbeat_seconds", 600.0, where),
        renotify_cooldown_seconds=_number(raw, "renotify_cooldown_seconds", 900.0, where),
        escalate_repeats=_integer(raw, "escalate_repeats", 1, where),
        escalate_interval_seconds=_number(raw, "escalate_interval_seconds", 5.0, where),
        max_concurrency=_integer(raw, "max_concurrency", 1, where),
        status_every_ticks=_integer(raw, "status_every_ticks", 0, where),
        notify_heartbeat_seconds=_number(raw, "notify_heartbeat_seconds", 0.0, where),
        open_url_on_hit=_boolean(raw, "open_url_on_hit", False, where),
        command_on_hit=(str(raw["command_on_hit"]) if raw.get("command_on_hit") else None),
        notify_kinds=_parse_event_kinds(raw, where),
        channels=_parse_channels(raw, where),
    )
    return _apply_env(settings, base)


def _apply_env(settings: Settings, base: Path) -> Settings:
    overrides: dict[str, Any] = {}
    numeric = {
        "INTERVAL_SECONDS": ("interval_seconds", float),
        "JITTER_RATIO": ("jitter_ratio", float),
        "MIN_HOST_INTERVAL_SECONDS": ("min_host_interval_seconds", float),
        "MAX_REQUESTS_PER_MINUTE": ("max_requests_per_minute", int),
        "TIMEOUT_SECONDS": ("timeout_seconds", float),
        "RETRIES": ("retries", int),
        "RENOTIFY_COOLDOWN_SECONDS": ("renotify_cooldown_seconds", float),
        "ESCALATE_REPEATS": ("escalate_repeats", int),
        "ESCALATE_INTERVAL_SECONDS": ("escalate_interval_seconds", float),
        "MAX_CONCURRENCY": ("max_concurrency", int),
        "NOTIFY_HEARTBEAT_SECONDS": ("notify_heartbeat_seconds", float),
    }
    for env_key, (field_name, caster) in numeric.items():
        raw = _env(env_key)
        if raw is None:
            continue
        try:
            overrides[field_name] = caster(raw)
        except ValueError:
            raise ConfigError(f"环境变量 {ENV_PREFIX}{env_key}={raw!r} 不是合法 {caster.__name__}") from None

    if (raw_db := _env("STATE_DB")) is not None:
        overrides["state_db"] = raw_db
    if (raw_proxy := _env("HTTP_PROXY")) is not None:
        overrides["http_proxy"] = raw_proxy
    if settings.state_db:
        overrides["state_db"] = _resolve_path(str(overrides.get("state_db", settings.state_db)), base)
    return replace(settings, **overrides) if overrides else settings


def _render_url(url: str, params: dict[str, Any], where: str) -> str:
    if "{" not in url:
        return url
    try:
        return url.format(**params)
    except KeyError as exc:
        raise ConfigError(f"{where} 缺少参数 {exc}；请在 match.params 里补上") from None
    except (IndexError, ValueError) as exc:
        raise ConfigError(f"{where} 无法渲染：{exc}") from None


def _parse_target(item: Any, index: int, where: str, base: Path) -> Target:
    if not isinstance(item, dict):
        raise ConfigError(f"{where} 的 targets[{index}] 需要字典，实际是 {item!r}")

    ident = str(item.get("id") or "").strip()
    if not ident:
        raise ConfigError(f"{where} 的 targets[{index}] 缺少 id")
    name = str(item.get("name") or ident)

    kind = str(item.get("kind") or "")
    if kind not in SOURCE_KINDS:
        allowed = ", ".join(sorted(SOURCE_KINDS))
        raise ConfigError(f"目标 {ident} 的 kind={kind!r} 不支持（可选：{allowed}）")

    match = item.get("match") or {}
    if not isinstance(match, dict):
        raise ConfigError(f"目标 {ident} 的 match 需要字典，实际是 {match!r}")
    match = dict(match)

    url = str(item.get("url") or "")
    if kind == "mock":
        # mock 源不联网，url 仅作展示；但为了让看板/日志可读，缺省给个占位。
        url = url or f"mock://{ident}"
        inventory = str(match.get("inventory_file") or base / "mock-inventory.json")
        match["inventory_file"] = _resolve_path(inventory, base)
    if not url:
        raise ConfigError(f"目标 {ident} 缺少 url")

    params = {"id": ident, "name": name}
    extra = match.get("params") or {}
    if not isinstance(extra, dict):
        raise ConfigError(f"目标 {ident} 的 match.params 需要字典，实际是 {extra!r}")
    params.update({str(k): v for k, v in extra.items()})
    url = _render_url(url, params, f"目标 {ident} 的 url 模板")
    buy_url = item.get("buy_url")
    if buy_url:
        buy_url = _render_url(str(buy_url), params, f"目标 {ident} 的 buy_url 模板")

    notify = item.get("notify") or ()
    if isinstance(notify, str):
        notify = (notify,)
    if not isinstance(notify, (list, tuple)):
        raise ConfigError(f"目标 {ident} 的 notify 需要列表，实际是 {notify!r}")

    interval = item.get("interval_seconds")
    if interval is not None:
        try:
            interval = float(interval)
        except (TypeError, ValueError):
            raise ConfigError(f"目标 {ident} 的 interval_seconds 需要数字，实际是 {interval!r}") from None

    return Target(
        id=ident,
        name=name,
        kind=kind,
        url=url,
        enabled=bool(item.get("enabled", True)),
        interval_seconds=interval,
        buy_url=str(buy_url) if buy_url else None,
        notify=tuple(str(x) for x in notify),
        match=match,
    )


def load_targets(
    path: str | os.PathLike[str] | None = None, *, base_dir: Path | None = None
) -> tuple[Target, ...]:
    base = base_dir or Path.cwd()
    target_path = Path(path) if path else base / "targets.yaml"
    raw = _read_yaml(target_path)
    where = target_path.name

    items = raw.get("targets") if isinstance(raw, dict) else raw
    if items is None:
        raise ConfigError(f"{where} 里没有任何目标")
    if not isinstance(items, (list, tuple)):
        raise ConfigError(f"{where} 的 targets 需要列表，实际是 {type(items).__name__}")

    targets: list[Target] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        target = _parse_target(item, index, where, base)
        if target.id in seen:
            raise ConfigError(f"{where} 里 id 重复：{target.id}")
        seen.add(target.id)
        targets.append(target)
    if not targets:
        raise ConfigError(f"{where} 里没有任何目标")
    return tuple(targets)


def load_bundle(config_dir: str | os.PathLike[str] | None = None) -> Bundle:
    base = resolve_config_dir(config_dir)
    # 相对路径（状态库、mock 库存文件）一律按**项目根**解析，即配置目录的父级。
    root = base.parent
    settings = load_settings(base / "settings.yaml", base_dir=root)
    targets = load_targets(base / "targets.yaml", base_dir=root)
    return Bundle(settings=settings, targets=targets, config_dir=base)
