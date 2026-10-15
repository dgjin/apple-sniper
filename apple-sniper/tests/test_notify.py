"""通知渠道：载荷格式、失败隔离、命令传递方式。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from helpers import make_target, run

from apple_sniper.config import ConfigError
from apple_sniper.models import Event, EventKind
from apple_sniper.notify import MultiNotifier, build_notifier, format_event, title_for
from apple_sniper.notify.channels import (
    CommandChannel,
    ConsoleChannel,
    MacosChannel,
    WebhookChannel,
    build_channel,
)

EVENT = Event(
    target_id="t1",
    kind=EventKind.BACK_IN_STOCK,
    at=datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc),
    message="已到货，价格 ¥9,499.00",
    price=9499.0,
)
TARGET = make_target("t1", "mock", name="演示 iPhone", buy_url="https://example.com/buy/t1")
OTHER_EVENT = Event(
    target_id="t1",
    kind=EventKind.OUT_OF_STOCK,
    at=EVENT.at,
    message="已无货",
)
# 心跳是「汇总」通知，没有具体商品页可跳，action_url 为空。
HEARTBEAT_EVENT = Event(
    target_id="heartbeat",
    kind=EventKind.HEARTBEAT,
    at=EVENT.at,
    message="监控运行中：6 个目标（无货 6）",
)
NO_LINK_TARGET = make_target("heartbeat", "mock", name="apple-sniper", url="")


class RecordingChannel:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[EventKind] = []

    async def send(self, event: Event, target) -> None:
        self.sent.append(event.kind)


class ExplodingChannel:
    name = "exploding"

    async def send(self, event: Event, target) -> None:
        raise RuntimeError("机器人挂了")


def test_format_event_mentions_target_and_link() -> None:
    text = format_event(EVENT, TARGET)
    assert "已到货 · 演示 iPhone（t1）" in text
    assert "已到货，价格 ¥9,499.00" in text
    assert "https://example.com/buy/t1" in text


def test_format_event_omits_link_when_target_has_none() -> None:
    text = format_event(HEARTBEAT_EVENT, NO_LINK_TARGET)
    assert "心跳 · apple-sniper（heartbeat）" in text
    assert "监控运行中：6 个目标（无货 6）" in text
    # 没有可跳转页面时不应出现空链接行
    assert len(text.splitlines()) == 2


def test_console_channel_writes() -> None:
    lines: list[str] = []
    run(ConsoleChannel(writer=lines.append).send(EVENT, TARGET))
    assert len(lines) == 1
    assert "演示 iPhone" in lines[0]


class TestMultiNotifier:
    def test_throttled_kinds_are_skipped(self) -> None:
        channel = RecordingChannel()
        notifier = MultiNotifier([channel], kinds={EventKind.BACK_IN_STOCK})

        async def scenario() -> tuple[bool, bool]:
            first = await notifier.send(EVENT, TARGET)
            second = await notifier.send(OTHER_EVENT, TARGET)
            return first, second

        first, second = run(scenario())
        assert first is True and second is False
        assert channel.sent == [EventKind.BACK_IN_STOCK]

    def test_channel_failure_is_isolated(self) -> None:
        good = RecordingChannel()
        notifier = MultiNotifier([ExplodingChannel(), good])

        delivered = run(notifier.send(EVENT, TARGET))
        assert delivered is True
        assert good.sent == [EventKind.BACK_IN_STOCK]
        assert len(notifier.failures) == 1
        assert "机器人挂了" in notifier.failures[0]

    def test_all_channels_failing_reports_not_delivered(self) -> None:
        notifier = MultiNotifier([ExplodingChannel()])
        assert run(notifier.send(EVENT, TARGET)) is False


class TestWebhookChannel:
    def capture(self, status: int = 200):
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(status, text="ok")

        return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen

    @staticmethod
    def body_client(payload: Any, status: int = 200) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json=payload)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def test_generic_payload(self) -> None:
        client, seen = self.capture()

        async def scenario() -> None:
            try:
                await WebhookChannel("https://hook.test/x", client=client).send(EVENT, TARGET)
            finally:
                await client.aclose()

        run(scenario())
        assert seen[0] == {
            "event": "back_in_stock",
            "event_label": "已到货",
            "target_id": "t1",
            "target_name": "演示 iPhone",
            "message": "已到货，价格 ¥9,499.00",
            "price": 9499.0,
            "url": "https://example.com/buy/t1",
            "at": "2026-10-15T12:00:00+00:00",
        }

    @pytest.mark.parametrize(
        ("template", "keys"),
        [
            ("bark", {"title", "body", "url", "group", "level"}),
            ("feishu", {"msg_type", "content"}),
            ("wecom", {"msgtype", "markdown"}),
            ("dingtalk", {"msgtype", "markdown"}),
            ("slack", {"text"}),
        ],
    )
    def test_known_templates_shape(self, template: str, keys: set[str]) -> None:
        client, seen = self.capture()

        async def scenario() -> None:
            try:
                channel = WebhookChannel("https://hook.test/x", template=template, client=client)
                await channel.send(EVENT, TARGET)
            finally:
                await client.aclose()

        run(scenario())
        assert set(seen[0]) == keys

    @pytest.mark.parametrize("template", ["feishu", "wecom", "dingtalk", "slack"])
    def test_heartbeat_payload_has_no_link(self, template: str) -> None:
        client, seen = self.capture()

        async def scenario() -> None:
            try:
                channel = WebhookChannel("https://hook.test/x", template=template, client=client)
                await channel.send(HEARTBEAT_EVENT, NO_LINK_TARGET)
            finally:
                await client.aclose()

        run(scenario())
        text = json.dumps(seen[0], ensure_ascii=False)
        assert "监控运行中" in text
        # 空的 action_url 既不该拼出空链接行，也不该冒出「立即查看」
        assert "立即查看" not in text
        assert "https://" not in text

    def test_feishu_heartbeat_text_has_no_trailing_blank_line(self) -> None:
        client, seen = self.capture()

        async def scenario() -> None:
            try:
                channel = WebhookChannel("https://hook.test/x", template="feishu", client=client)
                await channel.send(HEARTBEAT_EVENT, NO_LINK_TARGET)
            finally:
                await client.aclose()

        run(scenario())
        assert seen[0]["content"]["text"] == "apple-sniper · 心跳：监控运行中：6 个目标（无货 6）"

    def test_bark_payload_omits_url_key_without_link(self) -> None:
        client, seen = self.capture()

        async def scenario() -> None:
            try:
                channel = WebhookChannel("https://hook.test/x", template="bark", client=client)
                await channel.send(HEARTBEAT_EVENT, NO_LINK_TARGET)
            finally:
                await client.aclose()

        run(scenario())
        assert "url" not in seen[0]
        assert seen[0]["title"] == "apple-sniper · 心跳"

    def test_extra_fields_are_merged(self) -> None:
        client, seen = self.capture()

        async def scenario() -> None:
            try:
                channel = WebhookChannel(
                    "https://hook.test/x", extra={"device_key": "abc"}, client=client
                )
                await channel.send(EVENT, TARGET)
            finally:
                await client.aclose()

        run(scenario())
        assert seen[0]["device_key"] == "abc"

    def test_feishu_signature_added_when_secret_set(self) -> None:
        client, seen = self.capture()
        secret = "test-secret"

        async def scenario() -> None:
            try:
                channel = WebhookChannel(
                    "https://hook.test/x", template="feishu", secret=secret, client=client
                )
                await channel.send(EVENT, TARGET)
            finally:
                await client.aclose()

        run(scenario())
        payload = seen[0]
        assert set(payload) == {"msg_type", "content", "timestamp", "sign"}
        assert payload["timestamp"].isdigit()
        # 按飞书官方算法独立复算：key = "时间戳\n密钥"，消息体为空。
        string_to_sign = f"{payload['timestamp']}\n{secret}"
        expected = base64.b64encode(
            hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        ).decode("utf-8")
        assert payload["sign"] == expected

    def test_feishu_without_secret_has_no_signature(self) -> None:
        client, seen = self.capture()

        async def scenario() -> None:
            try:
                await WebhookChannel("https://hook.test/x", template="feishu", client=client).send(
                    EVENT, TARGET
                )
            finally:
                await client.aclose()

        run(scenario())
        assert set(seen[0]) == {"msg_type", "content"}

    def test_secret_rejected_for_non_feishu_template(self) -> None:
        with pytest.raises(ConfigError, match="只支持 feishu"):
            WebhookChannel("https://hook.test/x", template="wecom", secret="s")

    def test_http_error_raises(self) -> None:
        client, _ = self.capture(status=400)
        errors: list[str] = []

        async def scenario() -> None:
            try:
                channel = WebhookChannel("https://hook.test/x", client=client, on_error=errors.append)
                with pytest.raises(RuntimeError, match="HTTP 400"):
                    await channel.send(EVENT, TARGET)
            finally:
                await client.aclose()

        run(scenario())
        assert errors and "HTTP 400" in errors[0]

    def test_unknown_template_rejected(self) -> None:
        with pytest.raises(ConfigError, match="模板"):
            WebhookChannel("https://hook.test/x", template="telegram")

    def test_feishu_business_error_is_detected(self) -> None:
        """飞书失败时也回 HTTP 200，必须看 body.code，否则会把未送达当成功。"""
        client = self.body_client({"code": 19001, "msg": "param invalid"})
        errors: list[str] = []

        async def scenario() -> None:
            try:
                channel = WebhookChannel(
                    "https://hook.test/x", template="feishu", client=client, on_error=errors.append
                )
                with pytest.raises(RuntimeError, match="19001"):
                    await channel.send(EVENT, TARGET)
            finally:
                await client.aclose()

        run(scenario())
        assert errors and "19001" in errors[0]

    def test_feishu_success_body_passes(self) -> None:
        client = self.body_client({"code": 0, "msg": "success"})

        async def scenario() -> None:
            try:
                await WebhookChannel("https://hook.test/x", template="feishu", client=client).send(
                    EVENT, TARGET
                )
            finally:
                await client.aclose()

        run(scenario())  # 不抛异常即为通过

    @pytest.mark.parametrize(
        ("template", "payload"),
        [
            ("wecom", {"errcode": 93000, "errmsg": "invalid webhook url"}),
            ("dingtalk", {"errcode": 300001, "errmsg": "token is not exist"}),
            ("bark", {"code": 400, "message": "bad request"}),
            ("slack", {"ok": False, "error": "invalid_token"}),
        ],
    )
    def test_other_platform_business_errors_are_detected(
        self, template: str, payload: dict[str, Any]
    ) -> None:
        client = self.body_client(payload)

        async def scenario() -> None:
            try:
                channel = WebhookChannel("https://hook.test/x", template=template, client=client)
                with pytest.raises(RuntimeError, match="业务错误"):
                    await channel.send(EVENT, TARGET)
            finally:
                await client.aclose()

        run(scenario())

    def test_non_json_body_is_not_treated_as_failure(self) -> None:
        client, _ = self.capture()  # 返回纯文本 "ok"

        async def scenario() -> None:
            try:
                await WebhookChannel("https://hook.test/x", template="feishu", client=client).send(
                    EVENT, TARGET
                )
            finally:
                await client.aclose()

        run(scenario())

    def test_generic_template_ignores_body(self) -> None:
        """generic 没有约定业务码，响应体里出现 code 字段也不该误判。"""
        client = self.body_client({"code": 500})

        async def scenario() -> None:
            try:
                await WebhookChannel("https://hook.test/x", client=client).send(EVENT, TARGET)
            finally:
                await client.aclose()

        run(scenario())

    def test_injected_client_is_not_closed(self) -> None:
        client, _ = self.capture()

        async def scenario() -> None:
            channel = WebhookChannel("https://hook.test/x", client=client)
            await channel.send(EVENT, TARGET)
            await channel.aclose()
            # 注入的客户端由注入方管理：aclose 之后仍应可用。
            await channel.send(EVENT, TARGET)

        run(scenario())
        run(client.aclose())

    def test_self_created_client_is_closed(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, text="ok")

        transport = httpx.MockTransport(handler)

        async def scenario() -> None:
            channel = WebhookChannel("https://hook.test/x")
            channel._client = httpx.AsyncClient(transport=transport)
            await channel.send(EVENT, TARGET)
            assert channel._client is not None
            await channel.aclose()
            assert channel._client is None

        run(scenario())
        assert len(seen) == 1


class TestCommandChannel:
    def test_env_carries_event_fields(self) -> None:
        seen: list[tuple[list[str], dict[str, str]]] = []

        async def runner(argv: list[str], env: dict[str, str]) -> None:
            seen.append((argv, env))

        run(CommandChannel("/bin/echo hello world", runner=runner).send(EVENT, TARGET))
        argv, env = seen[0]
        assert argv == ["/bin/echo", "hello", "world"]
        assert env["APPLE_SNIPER_EVENT"] == "back_in_stock"
        assert env["APPLE_SNIPER_TARGET"] == "t1"
        assert env["APPLE_SNIPER_PRICE"] == "9499.0"
        assert env["APPLE_SNIPER_URL"] == "https://example.com/buy/t1"

    def test_message_with_shell_metacharacters_is_not_interpolated(self) -> None:
        """事件内容只通过环境变量传递，不进命令行——避免把页面内容拼成 shell 注入。"""
        seen: list[list[str]] = []

        async def runner(argv: list[str], env: dict[str, str]) -> None:
            seen.append(argv)

        risky = Event(
            target_id="t1",
            kind=EventKind.BACK_IN_STOCK,
            at=EVENT.at,
            message="; rm -rf / #",
        )
        run(CommandChannel("/bin/echo ok", runner=runner).send(risky, TARGET))
        assert seen[0] == ["/bin/echo", "ok"]

    def test_empty_command_rejected(self) -> None:
        with pytest.raises(ConfigError, match="不能为空"):
            CommandChannel("   ")


class TestMacosChannel:
    def test_disabled_off_darwin(self) -> None:
        calls: list[list[str]] = []

        async def runner(argv: list[str]) -> None:
            calls.append(argv)

        channel = MacosChannel(runner=runner, platform="linux")
        run(channel.send(EVENT, TARGET))
        assert calls == []
        assert channel.enabled is False

    def test_notifies_with_title_and_subtitle(self) -> None:
        calls: list[list[str]] = []

        async def runner(argv: list[str]) -> None:
            calls.append(argv)

        channel = MacosChannel(runner=runner, platform="darwin", sound="Glass")
        run(channel.send(EVENT, TARGET))
        assert calls[0][0] == "/usr/bin/osascript"
        script = calls[0][2]
        assert "display notification" in script
        # 副标题复用统一的 title_for 约定（目标名 · 事件），避免两处格式各写一遍。
        assert title_for(EVENT, TARGET) in script
        assert "演示 iPhone · 已到货" in script
        assert calls[1] == ["/usr/bin/afplay", "/System/Library/Sounds/Glass.aiff"]

    def test_quotes_and_newlines_are_escaped(self) -> None:
        calls: list[list[str]] = []

        async def runner(argv: list[str]) -> None:
            calls.append(argv)

        tricky = Event(
            target_id="t1",
            kind=EventKind.BACK_IN_STOCK,
            at=EVENT.at,
            message='包含 "引号" 与\n换行',
        )
        run(MacosChannel(runner=runner, platform="darwin").send(tricky, TARGET))
        script = calls[0][2]
        assert '\\"引号\\"' in script
        assert "\n" not in script

    def test_default_sound_maps_to_absolute_path(self) -> None:
        assert MacosChannel(sound="default").sound == "/System/Library/Sounds/Glass.aiff"
        assert MacosChannel(sound="/tmp/x.aiff").sound == "/tmp/x.aiff"


class TestBuildChannel:
    def test_unknown_option_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="不认识的参数"):
            build_channel("console", {"forme": 1})

    def test_webhook_requires_url(self) -> None:
        with pytest.raises(ConfigError, match="需要 url"):
            build_channel("webhook", {"template": "bark"})

    def test_command_requires_command(self) -> None:
        with pytest.raises(ConfigError, match="需要 command"):
            build_channel("command", {})

    def test_build_notifier_from_settings(self) -> None:
        from apple_sniper.config import ChannelConfig, Settings

        settings = Settings(
            channels=(ChannelConfig("console"), ChannelConfig("macos", {"sound": "default"})),
            notify_kinds=(EventKind.BACK_IN_STOCK,),
        )
        notifier = build_notifier(settings, writer=lambda _: None, platform="linux")
        assert [c.name for c in notifier.channels] == ["console", "macos"]
        assert run(notifier.send(OTHER_EVENT, TARGET)) is False
