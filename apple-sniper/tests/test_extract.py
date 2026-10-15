"""点路径提取与判定表达式。"""

from __future__ import annotations

import pytest

from apple_sniper.config import ConfigError
from apple_sniper.extract import (
    MISSING,
    as_number,
    build_predicate,
    coerce_bool,
    extract_path,
    tokenize,
)


class TestExtractPath:
    def test_nested_dict(self) -> None:
        data = {"a": {"b": {"c": 42}}}
        assert extract_path(data, "a.b.c") == 42

    def test_list_index(self) -> None:
        data = {"stores": [{"name": "A"}, {"name": "B"}]}
        assert extract_path(data, "stores[1].name") == "B"

    def test_negative_index(self) -> None:
        assert extract_path({"xs": [1, 2, 3]}, "xs[-1]") == 3

    def test_missing_key_returns_sentinel(self) -> None:
        assert extract_path({"a": {}}, "a.b") is MISSING

    def test_index_out_of_range_returns_sentinel(self) -> None:
        assert extract_path({"xs": [1]}, "xs[9]") is MISSING

    def test_traverse_into_scalar_returns_sentinel(self) -> None:
        assert extract_path({"a": 1}, "a.b") is MISSING

    def test_empty_path_returns_whole_document(self) -> None:
        data = {"a": 1}
        assert extract_path(data, "") is data
        assert extract_path(data, None) is data

    def test_tokenize_mixed(self) -> None:
        assert tokenize("a.b[0].c") == ["a", "b", 0, "c"]

    def test_missing_is_falsy(self) -> None:
        assert not MISSING


class TestAsNumber:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("¥5,999.00", 5999.0), ("$1,299", 1299.0), (1899, 1899.0), ("0", 0.0), (12.5, 12.5)],
    )
    def test_parses(self, raw: object, expected: float) -> None:
        assert as_number(raw) == expected

    @pytest.mark.parametrize("raw", [None, MISSING, "abc", "", True, {"a": 1}])
    def test_unparsable(self, raw: object) -> None:
        assert as_number(raw) is None


class TestCoerceBool:
    @pytest.mark.parametrize("raw", ["有货", "in stock", "true", "1", 1, True, ["x"]])
    def test_truthy(self, raw: object) -> None:
        assert coerce_bool(raw) is True

    @pytest.mark.parametrize("raw", ["无货", "out of stock", "sold out", "false", "0", 0, False, "", None, MISSING])
    def test_falsy(self, raw: object) -> None:
        assert coerce_bool(raw) is False


class TestPredicates:
    def test_exists(self) -> None:
        check = build_predicate("exists")
        assert check(1) and check(False) and check("")
        assert not check(None) and not check(MISSING)

    def test_not_exists(self) -> None:
        check = build_predicate("not_exists")
        assert check(None) and check(MISSING) and not check(0)

    def test_truthy(self) -> None:
        check = build_predicate("truthy")
        assert check("有货") and not check("无货")

    def test_non_empty(self) -> None:
        check = build_predicate("non_empty")
        assert check("x") and check(7) and not check("  ") and not check(MISSING)

    def test_equals_numeric_and_text(self) -> None:
        check = build_predicate("equals:true")
        assert check("true") and check("TRUE") and not check("false")
        assert build_predicate("equals:5")(5.0)
        assert build_predicate("equals:5")("5")

    def test_not_equals(self) -> None:
        assert build_predicate("not_equals:false")("true")

    def test_contains(self) -> None:
        check = build_predicate("contains:有货")
        assert check("现在有货了") and not check("暂无货")
        assert build_predicate("not_contains:售罄")("有货")

    def test_comparisons(self) -> None:
        assert build_predicate("gt:0")(3)
        assert not build_predicate("gt:0")(0)
        assert build_predicate("gte:0")(0)
        assert build_predicate("lt:10")("9.5")
        assert build_predicate("lte:10")(10)

    def test_comparison_on_unparsable_is_false(self) -> None:
        assert not build_predicate("gt:0")("暂无库存")

    def test_bool_spec(self) -> None:
        assert build_predicate(True)("有货")
        assert not build_predicate(False)("有货")

    def test_dict_spec(self) -> None:
        assert build_predicate({"op": "gt", "value": 2})(3)

    def test_unknown_op_raises(self) -> None:
        with pytest.raises(ConfigError, match="未知判定表达式"):
            build_predicate("between:1:2")

    def test_missing_value_raises(self) -> None:
        with pytest.raises(ConfigError, match="必须带值"):
            build_predicate("equals")

    def test_bad_threshold_raises(self) -> None:
        with pytest.raises(ConfigError, match="不是数字"):
            build_predicate("gt:abc")

    def test_bad_spec_type_raises(self) -> None:
        with pytest.raises(ConfigError, match="需要字符串或字典"):
            build_predicate(123)
