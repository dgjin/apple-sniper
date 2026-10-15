"""通知渠道实现。

约定：
- 渠道失败**只记录不外抛**（由 :class:`MultiNotifier` 兜住），
  一个 webhook 打不通不该让整轮监控崩掉；
- 命令渠道用 ``create_subprocess_exec`` 而不是 ``shell=True``，事件内容只通过
  环境变量传递，避免把页面内容拼进命令行造成注入。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import shlex
import sys
import time
from typing import Any, Awaitable, Callable

import httpx

from ..config import ConfigError
from ..models import Event, Target, to_iso

WEBHOOK_TEMPLATES = ("generic", "bark", "feishu", "wecom", "dingtalk", "slack")

# 这些平台即使业务失败也回 HTTP 200，只看状态码会把「没发出去」当成功。
# 键是模板名，值是「成功时该字段应有的值」。
BODY_ERROR_CHECKS: dict[str, dict[str, Any]] = {
    "feishu": {"code": 0, "StatusCode": 0},
    "wecom": {"errcode": 0},
    "dingtalk": {"errcode": 0},
    "bark": {"code": 200},
    "slack": {"ok": True},
}

DEFAULT_MACOS_SOUND = "/System/Library/Sounds/Glass.aiff"


def title_for(event: Event, target: Target) -> str:
    return f"{target.name} · {event.kind.label}"


def format_event(event: Event, target: Target) -> str:
    lines = [
        f"[{to_iso(event.at)}] {event.kind.label} · {target.name}（{event.target_id}）",
        f"  {event.message}",
    ]
    if target.action_url:
        lines.append(f"  {target.action_url}")
    return "\n".join(lines)


def _pick(options: dict[str, Any], allowed: set[str], where: str) -> dict[str, Any]:
    unknown = set(options) - allowed
    if unknown:
        raise ConfigError(
            f"{where} 不认识的参数 {', '.join(sorted(unknown))}（可用：{', '.join(sorted(allowed))}）"
        )
    return {k: v for k, v in options.items() if k in allowed}


class ConsoleChannel:
    name = "console"

    def __init__(self, *, writer: Callable[[str], Any] = print) -> None:
        self._write = writer

    async def send(self, event: Event, target: Target) -> None:
        self._write(format_event(event, target))


def _applescript(text: str) -> str:
    # AppleScript 字符串里换行/回车会截断语句，引号与反斜杠要转义。
    cleaned = text.replace("\\", "\\\\").replace('"', '\\"')
    cleaned = cleaned.replace("\r", " ").replace("\n", " ")
    return cleaned


class MacosChannel:
    """macOS 桌面通知 + 可选提示音；非 darwin 平台自动降级为空操作。"""

    name = "macos"

    def __init__(
        self,
        *,
        title: str = "apple-sniper",
        sound: str | None = None,
        runner: Callable[[list[str]], Awaitable[None]] | None = None,
        platform: str | None = None,
    ) -> None:
        self.title = title
        self.sound = self._sound_path(sound)
        self._run = runner or _run_quiet
        self.enabled = (platform or sys.platform) == "darwin"

    @staticmethod
    def _sound_path(sound: str | None) -> str | None:
        if not sound:
            return None
        if sound in {"default", "true", "on"}:
            return DEFAULT_MACOS_SOUND
        if sound.startswith("/"):
            return sound
        return f"/System/Library/Sounds/{sound}.aiff"

    async def send(self, event: Event, target: Target) -> None:
        if not self.enabled:
            return
        script = (
            f'display notification "{_applescript(event.message)}" '
            f'with title "{_applescript(self.title)}" '
            f'subtitle "{_applescript(title_for(event, target))}"'
        )
        await self._run(["/usr/bin/osascript", "-e", script])
        if self.sound:
            await self._run(["/usr/bin/afplay", self.sound])


def _payload(template: str, event: Event, target: Target) -> dict[str, Any]:
    title = title_for(event, target)
    url = target.action_url
    text = f"{title}：{event.message}"
    if template == "generic":
        return {
            "event": event.kind.value,
            "event_label": event.kind.label,
            "target_id": event.target_id,
            "target_name": target.name,
            "message": event.message,
            "price": event.price,
            "url": url,
            "at": to_iso(event.at),
        }
    # 心跳这类「没有可跳转页面」的通知 action_url 为空：不要拼出空链接行。
    if template == "bark":
        payload: dict[str, Any] = {
            "title": title,
            "body": event.message,
            "group": "apple-sniper",
            "level": "timeSensitive",
        }
        if url:
            payload["url"] = url
        return payload
    if template == "feishu":
        return {"msg_type": "text", "content": {"text": f"{text}\n{url}" if url else text}}
    if template == "wecom":
        body = f"**{title}**\n> {event.message}"
        return {"msgtype": "markdown", "markdown": {"content": f"{body}\n[立即查看]({url})" if url else body}}
    if template == "dingtalk":
        body = f"### {title}\n\n{event.message}"
        return {"msgtype": "markdown", "markdown": {"title": title, "text": f"{body}\n\n[立即查看]({url})" if url else body}}
    if template == "slack":
        return {"text": f"*{title}*\n{event.message}\n{url}" if url else f"*{title}*\n{event.message}"}
    raise ConfigError(f"未知 webhook 模板 {template!r}（可用：{', '.join(WEBHOOK_TEMPLATES)}）")


def _feishu_sign(secret: str, timestamp: str) -> str:
    """飞书自定义机器人的「签名校验」：以 ``时间戳\\n密钥`` 为 **key**、空消息做 HMAC-SHA256。

    注意与常见写法不同——Feishu 官方示例把拼接串当 key、消息体为空。
    """
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class WebhookChannel:
    name = "webhook"

    def __init__(
        self,
        url: str,
        *,
        template: str = "generic",
        extra: dict[str, Any] | None = None,
        secret: str | None = None,
        client: httpx.AsyncClient | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        if template not in WEBHOOK_TEMPLATES:
            raise ConfigError(f"webhook 模板 {template!r} 不支持（可用：{', '.join(WEBHOOK_TEMPLATES)}）")
        if secret and template != "feishu":
            raise ConfigError(f"secret（加签）目前只支持 feishu 模板，当前是 {template!r}")
        self.url = url
        self.template = template
        self.extra = dict(extra or {})
        self.secret = secret
        self._client = client
        # 外部注入的客户端由注入方负责关闭，避免共享客户端被某个渠道提前 aclose。
        self._owns_client = client is None
        self._on_error = on_error

    async def send(self, event: Event, target: Target) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
            self._owns_client = True
        payload = _payload(self.template, event, target)
        payload.update(self.extra)
        # 加签放在最后：timestamp 与 sign 必须成对，不能被 extra 拆散。
        if self.secret:
            timestamp = str(int(time.time()))
            payload["timestamp"] = timestamp
            payload["sign"] = _feishu_sign(self.secret, timestamp)
        response = await self._client.post(self.url, json=payload)
        if response.status_code >= 400:
            detail = response.text[:200]
            if self._on_error is not None:
                self._on_error(f"HTTP {response.status_code}：{detail}")
            raise RuntimeError(f"webhook 返回 HTTP {response.status_code}：{detail}")
        problem = self._body_error(response)
        if problem is not None:
            if self._on_error is not None:
                self._on_error(problem)
            raise RuntimeError(problem)

    def _body_error(self, response: httpx.Response) -> str | None:
        """读出响应体里的业务错误码（飞书/企微/钉钉等失败也回 HTTP 200）。

        只认已知模板、只认约定字段：字段不在或响应不是 JSON 就当成功，
        免得把 generic 模板的任意响应误判成失败。
        """
        checks = BODY_ERROR_CHECKS.get(self.template)
        if not checks:
            return None
        try:
            body = response.json()
        except ValueError:  # 非 JSON 响应无法判定，交给状态码说话
            return None
        if not isinstance(body, dict):
            return None
        for key, expected in checks.items():
            if key in body and body[key] != expected:
                return f"{self.template} 业务错误：{key}={body[key]!r}（HTTP {response.status_code}）"
        return None

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


async def _run_quiet(argv: list[str]) -> None:
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await process.wait()


async def _exec_command(argv: list[str], env: dict[str, str]) -> None:
    process = await asyncio.create_subprocess_exec(
        *argv, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()
    if process.returncode:
        raise RuntimeError(f"命令退出码 {process.returncode}：{stderr.decode(errors='replace')[:200]}")


class CommandChannel:
    name = "command"

    def __init__(
        self,
        command: str,
        *,
        runner: Callable[[list[str], dict[str, str]], Awaitable[None]] | None = None,
    ) -> None:
        argv = shlex.split(command)
        if not argv:
            raise ConfigError("command 渠道的 command 不能为空")
        self.argv = argv
        self._run = runner or _exec_command

    async def send(self, event: Event, target: Target) -> None:
        env = dict(os.environ)
        env.update(
            {
                "APPLE_SNIPER_EVENT": event.kind.value,
                "APPLE_SNIPER_EVENT_LABEL": event.kind.label,
                "APPLE_SNIPER_TARGET": event.target_id,
                "APPLE_SNIPER_TARGET_NAME": target.name,
                "APPLE_SNIPER_MESSAGE": event.message,
                "APPLE_SNIPER_PRICE": "" if event.price is None else f"{event.price}",
                "APPLE_SNIPER_URL": target.action_url,
                "APPLE_SNIPER_AT": to_iso(event.at),
            }
        )
        await self._run(self.argv, env)


def build_channel(
    kind: str,
    options: dict[str, Any],
    *,
    writer: Callable[[str], Any] = print,
    macos_runner: Callable[[list[str]], Awaitable[None]] | None = None,
    command_runner: Callable[[list[str], dict[str, str]], Awaitable[None]] | None = None,
    platform: str | None = None,
    webhook_client: httpx.AsyncClient | None = None,
    on_error: Callable[[str], None] | None = None,
) -> Any:
    where = f"channels[{kind}]"
    if kind == "console":
        _pick(options, set(), where)
        return ConsoleChannel(writer=writer)
    if kind == "macos":
        opts = _pick(options, {"title", "sound"}, where)
        return MacosChannel(runner=macos_runner, platform=platform, **opts)
    if kind == "webhook":
        opts = _pick(options, {"url", "template", "extra", "secret"}, where)
        url = opts.pop("url", None)
        if not url:
            raise ConfigError(f"{where} 需要 url")
        return WebhookChannel(str(url), client=webhook_client, on_error=on_error, **opts)
    if kind == "command":
        opts = _pick(options, {"command"}, where)
        command = opts.pop("command", None)
        if not command:
            raise ConfigError(f"{where} 需要 command")
        return CommandChannel(str(command), runner=command_runner)
    raise ConfigError(f"未实现的通知渠道 {kind!r}")
