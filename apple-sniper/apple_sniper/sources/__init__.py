"""来源注册表。"""

from __future__ import annotations

from typing import Callable

from ..config import SOURCE_KINDS, ConfigError
from ..models import Target, utcnow
from .apple_fulfillment import AppleFulfillmentSource
from .base import Source, short
from .generic_json import GenericJsonSource
from .html_regex import HtmlRegexSource
from .mock import MockSource

_REGISTRY: dict[str, type[Source]] = {
    cls.kind: cls
    for cls in (MockSource, GenericJsonSource, HtmlRegexSource, AppleFulfillmentSource)
}


def _verify_registry() -> None:
    """registry 与 config 的合法取值必须一致，否则配置校验会放行一个根本不存在的能力。"""
    missing = SOURCE_KINDS - set(_REGISTRY)
    extra = set(_REGISTRY) - SOURCE_KINDS
    if missing or extra:
        raise RuntimeError(f"来源注册表与 SOURCE_KINDS 不一致：缺少 {missing or '-'}，多出 {extra or '-'}")


_verify_registry()


def create_source(target: Target, *, clock: Callable = utcnow) -> Source:
    try:
        cls = _REGISTRY[target.kind]
    except KeyError:
        allowed = ", ".join(sorted(_REGISTRY))
        raise ConfigError(f"目标 {target.id} 的来源类型 {target.kind!r} 未注册（可选：{allowed}）") from None
    return cls(target, clock=clock)


def source_kinds() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


__all__ = ["AppleFulfillmentSource", "GenericJsonSource", "HtmlRegexSource", "MockSource",
           "Source", "create_source", "short", "source_kinds"]
