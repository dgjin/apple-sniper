"""从响应里取值并判断是否「有货」。

路径语法够用即可，刻意不实现完整 JSONPath（避免引入依赖，也避免配置里出现
难以推理的表达式）：

    content.pickupMessage.stores[0].storeName
    data[2].sku
    items[-1].availability

判定表达式（写在 ``match.when`` / ``match.exists`` / ``match.present`` 处）：::

    exists | not_exists | non_empty | truthy
    equals:v | not_equals:v | contains:v | not_contains:v
    gt:n | gte:n | lt:n | lte:n

``truthy`` 会把「有货 / in stock / available / true / 1 / 非空字符串」都算作真，
把「无货 / out of stock / sold out / false / 0 / 空串 / null」算作假。
"""

from __future__ import annotations

import re
from typing import Any, Callable

from .config import ConfigError


class _Missing:
    _instance: "_Missing | None" = None

    def __new__(cls) -> "_Missing":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<MISSING>"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()

_TOKEN = re.compile(r"([^.\[\]]+)|\[(-?\d+)\]")

_FALSE_WORDS = frozenset(
    {
        "false",
        "no",
        "off",
        "0",
        "无货",
        "缺货",
        "暂时无货",
        "out of stock",
        "outofstock",
        "sold out",
        "soldout",
        "unavailable",
        "not available",
        "empty",
    }
)
_TRUE_WORDS = frozenset(
    {
        "true",
        "yes",
        "on",
        "1",
        "有货",
        "现货",
        "in stock",
        "instock",
        "available",
        "in_stock",
    }
)
_CURRENCY = re.compile(r"[^0-9.\-]")


def tokenize(path: str) -> list[str | int]:
    tokens: list[str | int] = []
    for match in _TOKEN.finditer(path):
        name, index = match.group(1), match.group(2)
        tokens.append(name if name is not None else int(index))
    return tokens


def extract_path(data: Any, path: str | None) -> Any:
    """按点路径取值；任何一段取不到都返回 :data:`MISSING`（不抛异常）。"""
    if not path:
        return data
    current = data
    for token in tokenize(path):
        if isinstance(token, int):
            if not isinstance(current, (list, tuple)):
                return MISSING
            try:
                current = current[token]
            except IndexError:
                return MISSING
            continue
        if isinstance(current, dict):
            if token not in current:
                return MISSING
            current = current[token]
            continue
        return MISSING
    return current


def coerce_bool(value: Any) -> bool:
    if value is MISSING or value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _FALSE_WORDS:
            return False
        if text in _TRUE_WORDS:
            return True
        return bool(text)
    return bool(value)


def as_number(value: Any) -> float | None:
    """把 ``"¥5,999.00"`` 这类文本转成数字；转不动就返回 None。"""
    if value is MISSING or value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = _CURRENCY.sub("", value)
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _split_spec(spec: Any) -> tuple[str, str | None]:
    if isinstance(spec, bool):
        return ("truthy" if spec else "not_exists"), None
    if isinstance(spec, str):
        op, _, raw = spec.partition(":")
        return op.strip().lower(), (raw if _ else None)
    if isinstance(spec, dict):
        op = str(spec.get("op") or "").strip().lower()
        raw = spec.get("value")
        return op, (None if raw is None else str(raw))
    raise ConfigError(f"判定表达式需要字符串或字典，实际是 {spec!r}")


def build_predicate(spec: Any) -> Callable[[Any], bool]:
    """把配置里的判定表达式编译成 ``(value) -> bool``。"""
    op, raw = _split_spec(spec)
    if op in {"exists", "not_missing"}:
        return lambda value: value is not MISSING and value is not None
    if op in {"not_exists", "missing"}:
        return lambda value: value is MISSING or value is None
    if op == "non_empty":
        return lambda value: bool(str(value).strip()) and value is not MISSING
    if op == "truthy":
        return coerce_bool
    if op in {"equals", "not_equals"}:
        if raw is None:
            raise ConfigError(f"判定表达式 {op} 必须带值，例如 {op}:true")
        expected = raw

        def _equal(value: Any, expected: str = expected) -> bool:
            number = as_number(value)
            other = as_number(expected)
            if number is not None and other is not None:
                return abs(number - other) < 1e-9
            return str(value).strip().lower() == expected.strip().lower()

        return _equal if op == "equals" else (lambda value: not _equal(value))
    if op in {"contains", "not_contains"}:
        if raw is None:
            raise ConfigError(f"判定表达式 {op} 必须带值，例如 {op}:有货")

        def _contains(value: Any, raw: str = raw) -> bool:
            text = "" if value is MISSING or value is None else str(value)
            return raw in text

        return _contains if op == "contains" else (lambda value: not _contains(value))
    if op in {"gt", "gte", "lt", "lte"}:
        if raw is None:
            raise ConfigError(f"判定表达式 {op} 必须带数字，例如 {op}:0")
        try:
            threshold = float(raw)
        except ValueError:
            raise ConfigError(f"判定表达式 {op} 的阈值不是数字：{raw!r}") from None

        def _compare(value: Any, op: str = op, threshold: float = threshold) -> bool:
            number = as_number(value)
            if number is None:
                return False
            if op == "gt":
                return number > threshold
            if op == "gte":
                return number >= threshold
            if op == "lt":
                return number < threshold
            return number <= threshold

        return _compare

    allowed = (
        "exists, not_exists, non_empty, truthy, equals:v, not_equals:v, "
        "contains:v, not_contains:v, gt:n, gte:n, lt:n, lte:n"
    )
    raise ConfigError(f"未知判定表达式 {op!r}（可用：{allowed}）")
