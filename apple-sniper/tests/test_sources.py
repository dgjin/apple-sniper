"""来源适配器：判定逻辑、失败归类、注册表。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from helpers import json_handler, make_fetcher, make_target, run

from apple_sniper.config import ConfigError
from apple_sniper.models import Availability
from apple_sniper.sources import create_source, source_kinds

IN_STOCK = Availability.IN_STOCK
OUT_OF_STOCK = Availability.OUT_OF_STOCK
UNKNOWN = Availability.UNKNOWN


def text_handler(body: str, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body, headers={"content-type": "text/html"})

    return handler


def forbidden(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 触发即失败
    raise AssertionError("该来源不应发起网络请求")


def check(target, handler=None, **fetch_kwargs):
    if handler is None:
        handler = forbidden if target.kind == "mock" else json_handler({})
    fetcher = make_fetcher(handler, **fetch_kwargs)

    async def scenario():
        try:
            return await create_source(target).check(fetcher)
        finally:
            await fetcher.aclose()

    return run(scenario())


class TestRegistry:
    def test_all_kinds_registered(self) -> None:
        assert source_kinds() == ("apple_fulfillment", "generic_json", "html_regex", "mock")

    def test_unknown_kind_reports_available(self) -> None:
        target = make_target("x", "rss", match={})
        with pytest.raises(ConfigError, match="未注册"):
            create_source(target)


class TestGenericJson:
    def target(self, **match):
        return make_target("gj", "generic_json", match=match)

    def test_in_stock(self) -> None:
        result = check(self.target(path="data.stock", when="gt:0"), json_handler({"data": {"stock": 3}}))
        assert result.ok and result.availability is IN_STOCK
        assert "data.stock=3" in result.detail

    def test_out_of_stock(self) -> None:
        result = check(self.target(path="data.stock", when="gt:0"), json_handler({"data": {"stock": 0}}))
        assert result.availability is OUT_OF_STOCK

    def test_exists_predicate(self) -> None:
        result = check(self.target(path="data.buyable", when="exists"), json_handler({"data": {"buyable": False}}))
        assert result.availability is IN_STOCK

    def test_price_is_parsed(self) -> None:
        payload = {"data": {"stock": 1, "price": "¥5,999.00"}}
        result = check(self.target(path="data.stock", when="gt:0", price_path="data.price"), json_handler(payload))
        assert result.price == 5999.0

    # 真实响应快照（2026-10-15 实测 Apple 中国）：
    # GET https://www.apple.com.cn/shop/sba/availability-message
    #     ?parts.0=MK2U4CH/A&store=R388&fae=true   → iPhone Duo 1TB 夜空色 / 西单大悦城
    APPLE_SBA = (
        '{"head":{"status":"200","data":{}},"body":{"pickupEnabled":true,"content":['
        '{"partNumber":"MK2U4CH/A","partAvailableStoresCount":0,"availableAtAnyStore":false,'
        '"deliveryMessage":{"deliveryOptionMessages":[{"displayName":"暂未发售"}],'
        '"subHeader":"iPhone Duo 1TB 夜空色",'
        '"buyability":{"reason":"NOT_FOR_SALE","isBuyable":false},"isBuyable":false},'
        '"distance":0.0}]}}'
    )
    SBA_PATH = "body.content[0].deliveryMessage.isBuyable"

    def test_apple_cn_sba_not_for_sale_is_out_of_stock(self) -> None:
        result = check(self.target(path=self.SBA_PATH, when="truthy"), json_handler(json.loads(self.APPLE_SBA)))
        assert result.ok and result.availability is OUT_OF_STOCK

    def test_apple_cn_sba_buyable_is_in_stock(self) -> None:
        payload = json.loads(self.APPLE_SBA)
        payload["body"]["content"][0]["deliveryMessage"]["isBuyable"] = True
        result = check(self.target(path=self.SBA_PATH, when="truthy"), json_handler(payload))
        assert result.ok and result.availability is IN_STOCK

    def test_apple_cn_sba_alternate_predicates_agree(self) -> None:
        # 配置里注释的两种备选判据：都改成「有货」时应与主判据结论一致。
        payload = json.loads(self.APPLE_SBA)
        payload["body"]["content"][0]["availableAtAnyStore"] = True
        payload["body"]["content"][0]["partAvailableStoresCount"] = 2
        by_flag = check(self.target(path="body.content[0].availableAtAnyStore", when="truthy"), json_handler(payload))
        by_count = check(self.target(path="body.content[0].partAvailableStoresCount", when="gt:0"), json_handler(payload))
        assert by_flag.availability is IN_STOCK and by_count.availability is IN_STOCK

    def test_missing_path_is_out_of_stock_with_hint(self) -> None:
        result = check(self.target(path="data.stock"), json_handler({"other": 1}))
        assert result.ok and result.availability is OUT_OF_STOCK
        assert "路径取不到值" in result.detail

    def test_http_error_is_failure_not_out_of_stock(self) -> None:
        result = check(self.target(path="data.stock"), text_handler("boom", status=500))
        assert not result.ok and result.availability is UNKNOWN
        assert "HTTP 500" in result.error

    def test_invalid_json_is_failure(self) -> None:
        result = check(self.target(path="data.stock"), text_handler("<html>登录页</html>"))
        assert not result.ok and "合法 JSON" in result.error

    def test_network_failure_is_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        result = check(self.target(path="data.stock"), handler)
        assert not result.ok and result.availability is UNKNOWN
        assert "请求失败" in result.error

    def test_path_required_at_construction(self) -> None:
        with pytest.raises(ConfigError, match="需要 match.path"):
            create_source(self.target())

    def test_bad_predicate_fails_at_construction(self) -> None:
        with pytest.raises(ConfigError, match="未知判定表达式"):
            create_source(self.target(path="data.stock", when="大概有货"))


class TestHtmlRegex:
    def target(self, **match):
        return make_target("hr", "html_regex", match=match, url="https://shop.test/item")

    def test_present_hit_means_in_stock(self) -> None:
        result = check(self.target(present="加入购物车"), text_handler("<button>加入购物车</button>"))
        assert result.availability is IN_STOCK
        assert "命中 present" in result.detail

    def test_present_miss_means_out_of_stock(self) -> None:
        result = check(self.target(present="加入购物车"), text_handler("<div>即将发售</div>"))
        assert result.availability is OUT_OF_STOCK

    def test_absent_hit_wins_over_present(self) -> None:
        body = "<button>加入购物车</button><span>暂无货</span>"
        result = check(self.target(present="加入购物车", absent="暂无货|已售罄"), text_handler(body))
        assert result.availability is OUT_OF_STOCK
        assert "命中 absent" in result.detail

    def test_absent_only_uses_negation(self) -> None:
        result = check(self.target(absent="暂无货"), text_handler("<div>有货</div>"))
        assert result.availability is IN_STOCK

    def test_price_regex_group(self) -> None:
        body = '{"price": "9499.00"}'
        result = check(self.target(present="price", price_regex=r'"price"\s*:\s*"?([0-9.]+)'), text_handler(body))
        assert result.price == 9499.0

    def test_requires_a_rule(self) -> None:
        with pytest.raises(ConfigError, match="至少需要 match.present"):
            create_source(self.target())

    def test_bad_regex_fails_at_construction(self) -> None:
        with pytest.raises(ConfigError, match="正则非法"):
            create_source(self.target(present="(["))

    def test_http_error_is_failure(self) -> None:
        result = check(self.target(present="加入购物车"), text_handler("限流", status=429))
        assert not result.ok and "HTTP 429" in result.error


APPLE_PAYLOAD = {
    "content": {
        "pickupMessage": {
            "stores": [
                {"storeName": "北京三里屯", "partsAvailability": {"MPXXXXCH/A": "available"}},
                {"storeName": "上海南京东路", "partsAvailability": {"MPXXXXCH/A": "unavailable"}},
            ]
        }
    }
}


class TestAppleFulfillment:
    def target(self, **match):
        match.setdefault("store_availability_path", "partsAvailability")
        match.setdefault("store_availability_key", "MPXXXXCH/A")
        return make_target("af", "apple_fulfillment", match=match, url="https://www.apple.com.cn/shop/fulfillment-messages")

    def test_finds_store_with_stock(self) -> None:
        result = check(self.target(), json_handler(APPLE_PAYLOAD))
        assert result.ok and result.availability is IN_STOCK
        assert "北京三里屯" in result.detail
        assert "上海南京东路" not in result.detail

    def test_all_stores_unavailable(self) -> None:
        payload = {
            "content": {
                "pickupMessage": {
                    "stores": [{"storeName": "北京三里屯", "partsAvailability": {"MPXXXXCH/A": "unavailable"}}]
                }
            }
        }
        result = check(self.target(), json_handler(payload))
        assert result.availability is OUT_OF_STOCK
        assert "有货：无" in result.detail

    def test_candidate_path_fallback(self) -> None:
        payload = {"body": {"content": {"pickupMessage": APPLE_PAYLOAD["content"]["pickupMessage"]}}}
        result = check(self.target(), json_handler(payload))
        assert result.availability is IN_STOCK
        assert "body.content.pickupMessage.stores" in result.detail

    def test_missing_all_paths_is_failure_with_hint(self) -> None:
        result = check(self.target(), json_handler({"unexpected": True}))
        assert not result.ok
        assert "候选路径" in result.error
        assert "probe" in result.error

    def test_without_availability_path_uses_predicate(self) -> None:
        target = self.target(store_availability_path=None, store_availability_key=None, when="truthy")
        result = check(target, json_handler(APPLE_PAYLOAD))
        assert result.availability is IN_STOCK
        assert "未配置 store_availability_path" in result.detail

    def test_empty_paths_rejected(self) -> None:
        with pytest.raises(ConfigError, match="不能为空列表"):
            create_source(self.target(paths=[]))

    def test_paths_must_be_list(self) -> None:
        with pytest.raises(ConfigError, match="match.paths 需要列表"):
            create_source(self.target(paths="content.stores"))


class TestMockSource:
    def write(self, tmp_path: Path, payload) -> Path:
        path = tmp_path / "inventory.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def target(self, tmp_path: Path, ident: str = "m1"):
        return make_target(ident, "mock", match={"inventory_file": str(tmp_path / "inventory.json")})

    def test_available(self, tmp_path: Path) -> None:
        self.write(tmp_path, {"m1": {"available": True, "price": 9999, "detail": "演示补货"}})
        result = check(self.target(tmp_path))
        assert result.availability is IN_STOCK and result.price == 9999.0
        assert result.detail == "演示补货"

    def test_unavailable(self, tmp_path: Path) -> None:
        self.write(tmp_path, {"m1": {"available": False}})
        assert check(self.target(tmp_path)).availability is OUT_OF_STOCK

    def test_explicit_error_entry(self, tmp_path: Path) -> None:
        self.write(tmp_path, {"m1": {"error": "模拟接口 503"}})
        result = check(self.target(tmp_path))
        assert not result.ok and "模拟接口 503" in result.error

    def test_missing_file_is_failure(self, tmp_path: Path) -> None:
        result = check(self.target(tmp_path))
        assert not result.ok and "库存文件不存在" in result.error

    def test_unlisted_target_is_out_of_stock(self, tmp_path: Path) -> None:
        self.write(tmp_path, {"other": {"available": True}})
        result = check(self.target(tmp_path))
        assert result.availability is OUT_OF_STOCK
        assert "未列出该目标" in result.detail

    def test_invalid_json_is_failure(self, tmp_path: Path) -> None:
        (tmp_path / "inventory.json").write_text("{ nope", encoding="utf-8")
        result = check(self.target(tmp_path))
        assert not result.ok and "合法 JSON" in result.error

    def test_top_level_shorthand(self, tmp_path: Path) -> None:
        self.write(tmp_path, {"available": True, "price": 1899})
        result = check(self.target(tmp_path))
        assert result.availability is IN_STOCK and result.price == 1899.0

    def test_never_touches_network(self, tmp_path: Path) -> None:
        self.write(tmp_path, {"m1": {"available": True}})
        check(self.target(tmp_path), forbidden)
