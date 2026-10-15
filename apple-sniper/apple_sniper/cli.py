"""命令行入口。

    apple-sniper targets                 列出目标
    apple-sniper probe  <目标> [--chars] 打印真实响应，核对字段路径用
    apple-sniper check  [--target ...]   跑一轮并落库/通知（适合 cron）
    apple-sniper drill  [--act]          用真实渠道发一条合成的「已到货」，验证通知链路
    apple-sniper watch  [--max-ticks N]  持续轮询
    apple-sniper history [-n N]          查看事件历史
    apple-sniper forget --target ...     清空指定目标的历史（重新开始监控）
    apple-sniper serve  [--port 8787]    本地只读看板
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import unicodedata
from typing import Any, Sequence

from . import __version__
from .config import Bundle, ChannelConfig, ConfigError, load_bundle
from .engine import Hit, Sniper
from .http import FetchError, Fetcher, build_client, build_limiter
from .models import Event, EventKind, Target, to_iso, utcnow
from .notify import build_notifier
from .sources import create_source
from .store import Store, forget

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


# --- 终端表格（按东亚字符宽度对齐） ---


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, target_width: int) -> str:
    return text + " " * max(0, target_width - _width(text))


def print_table(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> None:
    widths = [max(_width(str(headers[i])), *(_width(str(row[i])) for row in rows)) for i in range(len(headers))]
    header_line = "  ".join(_pad(str(h), widths[i]) for i, h in enumerate(headers))
    print(header_line.rstrip())
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(_pad(str(cell), widths[i]) for i, cell in enumerate(row)).rstrip())


# --- 公共装配 ---


def _select_targets(bundle: Bundle, ids: Sequence[str] | None) -> list[Target]:
    if not ids:
        return list(bundle.targets)
    index = {target.id: target for target in bundle.targets}
    unknown = [ident for ident in ids if ident not in index]
    if unknown:
        available = ", ".join(index)
        raise ConfigError(f"未知目标 {', '.join(unknown)}（配置里有：{available}）")
    return [index[ident] for ident in ids]


def _build_runtime(bundle: Bundle, reporter: Any = None) -> tuple[Fetcher, Sniper]:
    settings = bundle.settings
    client = build_client(settings)
    fetcher = Fetcher(
        client,
        limiter=build_limiter(settings),
        retries=settings.retries,
        backoff_base_seconds=settings.backoff_base_seconds,
    )
    store = Store(settings.state_db)
    notifier = build_notifier(settings, webhook_client=client)
    engine = Sniper(bundle.targets, settings, store, notifier, fetcher, reporter=reporter)
    return fetcher, engine


def _target_line(target: Target, settings_interval: float) -> str:
    interval = target.interval_seconds or settings_interval
    state = "启用" if target.enabled else "停用"
    return f"{state} 每 {interval:g}s"


# --- 各子命令 ---


def cmd_targets(args: argparse.Namespace) -> int:
    bundle = load_bundle(args.config_dir)
    rows = [
        [
            target.id,
            target.name,
            target.kind,
            _target_line(target, bundle.settings.interval_seconds),
            target.url,
        ]
        for target in bundle.targets
    ]
    print_table(rows, ["目标", "名称", "来源", "节奏", "URL"])
    print()
    print(f"配置目录 {bundle.config_dir}")
    print(f"状态库   {bundle.settings.state_db}")
    print(f"通知渠道 {'、'.join(c.kind for c in bundle.settings.channels)}")
    print(f"通知事件 {'、'.join(k.label for k in bundle.settings.notify_kinds)}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    bundle = load_bundle(args.config_dir)
    targets = _select_targets(bundle, args.target)
    return asyncio.run(_probe_async(bundle, targets, args))


async def _probe_async(bundle: Bundle, targets: Sequence[Target], args: argparse.Namespace) -> int:
    settings = bundle.settings
    client = build_client(settings)
    fetcher = Fetcher(
        client,
        limiter=build_limiter(settings),
        retries=settings.retries,
        backoff_base_seconds=settings.backoff_base_seconds,
    )
    exit_code = 0
    try:
        for target in targets:
            source = create_source(target)
            print(f"== {target.id} · {target.name} · 来源 {target.kind}")
            print(f"   URL {target.url}")
            if target.url.startswith(("http://", "https://")):
                try:
                    response = await fetcher.get(target.url)
                except FetchError as exc:
                    print(f"   抓取失败：{exc}")
                    exit_code = 1
                    continue
                print(
                    f"   HTTP {response.status} · {response.elapsed_ms:.0f}ms · "
                    f"尝试 {response.attempts} 次 · Content-Type "
                    f"{response.headers.get('content-type', '未知')}"
                )
                body = response.text
                print(f"   正文 {len(body)} 字符，前 {args.chars} 字符：")
                for line in body[: args.chars].splitlines() or [""]:
                    print(f"   | {line}")
            else:
                print("   （本地来源，不联网）")

            # 判定用的是来源自己的抓取/解析，因此这里会再请求一次——
            # 目的正是验证「配置里写的路径」能否得出正确结论。
            result = await source.check(fetcher)
            verdict = "有货" if result.availability.value == "in_stock" else (
                "无货" if result.availability.value == "out_of_stock" else "未知"
            )
            flag = "OK " if result.ok else "ERR"
            print(f"   {flag} 判定={verdict} 价格={result.price} 详情={result.detail or '-'}")
            if not result.ok:
                print(f"       错误：{result.error}")
                exit_code = 1
            print()
    finally:
        await client.aclose()
    return exit_code


def cmd_check(args: argparse.Namespace) -> int:
    bundle = load_bundle(args.config_dir)
    targets = _select_targets(bundle, args.target)
    return asyncio.run(_check_async(bundle, targets, args))


async def _check_async(bundle: Bundle, targets: Sequence[Target], args: argparse.Namespace) -> int:
    fetcher, engine = _build_runtime(bundle)
    rows: list[list[str]] = []
    payload: list[dict[str, Any]] = []
    failed = 0
    try:
        for target in targets:
            hit = await engine.check_one(target)
            rows.append(
                [
                    target.id,
                    hit.result.availability.label,
                    "-" if hit.result.price is None else f"¥{hit.result.price:,.2f}",
                    "、".join(event.kind.label for event in hit.events) or "-",
                    (hit.result.detail or hit.result.error or "-")[:60],
                ]
            )
            payload.append(
                {
                    "target": target.id,
                    "ok": hit.result.ok,
                    "availability": hit.result.availability.value,
                    "price": hit.result.price,
                    "detail": hit.result.detail,
                    "error": hit.result.error,
                    "at": to_iso(hit.result.observed_at),
                    "events": [
                        {"kind": e.kind.value, "message": e.message, "at": to_iso(e.at)} for e in hit.events
                    ],
                    "notified": hit.notified,
                }
            )
            if not hit.result.ok:
                failed += 1
        await engine.heartbeat()
    finally:
        await engine.notifier.aclose()
        await fetcher.aclose()

    if args.json:
        print(json.dumps({"results": payload, "failed": failed}, ensure_ascii=False, indent=2))
    else:
        print_table(rows, ["目标", "状态", "价格", "本次事件", "详情"])
        print()
        print(f"共 {len(targets)} 个目标，抓取失败 {failed} 个；状态已落库 {bundle.settings.state_db}")
    return 1 if failed else 0


def cmd_drill(args: argparse.Namespace) -> int:
    bundle = load_bundle(args.config_dir)
    targets = _select_targets(bundle, [args.target] if args.target else None)
    enabled = [target for target in targets if target.enabled]
    target = enabled[0] if enabled else targets[0]
    return asyncio.run(_drill_async(bundle, target, args))


def _channel_label(index: int, config: ChannelConfig) -> str:
    if config.kind == "webhook":
        template = config.options.get("template", "generic")
        return f"channels[{index}] webhook/{template}"
    return f"channels[{index}] {config.kind}"


async def _drill_async(bundle: Bundle, target: Target, args: argparse.Namespace) -> int:
    """发一条**合成**的「已到货」事件，验证通知渠道与命中动作是否真的通。

    刻意绕开 :class:`MultiNotifier` 的 ``notify_kinds`` 过滤逐个渠道试，
    这样「渠道没配好」和「事件没订阅」这两类问题不会混在一起。
    """
    settings = bundle.settings
    if EventKind.BACK_IN_STOCK not in settings.notify_kinds:
        print(
            "提醒：notify_kinds 里没有 back_in_stock，真实命中时这些渠道不会推送。",
            file=sys.stderr,
        )

    client = build_client(settings)
    notifier = build_notifier(settings, webhook_client=client)
    store = Store(settings.state_db)
    fetcher = Fetcher(
        client,
        limiter=build_limiter(settings),
        retries=settings.retries,
        backoff_base_seconds=settings.backoff_base_seconds,
    )
    engine = Sniper(bundle.targets, settings, store, notifier, fetcher)

    event = Event(
        target_id=target.id,
        kind=EventKind.BACK_IN_STOCK,
        at=utcnow(),
        message=args.message or f"【演练】{target.name} 已到货（合成事件，非真实抓取）",
        url=target.buy_url,
    )

    print(f"演练目标 {target.id} · {target.name}")
    print(f"合成事件 {event.kind.label}：{event.message}")
    print()

    failures = 0
    try:
        for index, config in enumerate(settings.channels):
            label = _channel_label(index, config)
            try:
                await notifier.channels[index].send(event, target)
            except Exception as exc:  # 演练就是要逐渠道把失败摊开
                failures += 1
                print(f"  ✗ {label}：{type(exc).__name__}: {exc}")
            else:
                print(f"  ✓ {label}")

        if args.act:
            print()
            action = await engine.apply_hit_actions(target, (event,))
            print(f"命中动作：{action or '（open_url_on_hit / command_on_hit 都没开）'}")
    finally:
        await notifier.aclose()
        await fetcher.aclose()
        store.close()

    print()
    print(f"渠道 {len(settings.channels)} 个，失败 {failures} 个")
    return 1 if failures else 0


def cmd_watch(args: argparse.Namespace) -> int:
    bundle = load_bundle(args.config_dir)
    return asyncio.run(_watch_async(bundle, args))


def _describe_hit(hit: Hit) -> None:
    stamp = to_iso(hit.result.observed_at)[11:19]
    marks = "、".join(event.kind.label for event in hit.events) or "无变化"
    note = hit.result.detail or hit.result.error or ""
    action = f" | {hit.action}" if hit.action else ""
    stream = sys.stdout
    stream.write(
        f"[{stamp}] {_pad(hit.target.id, 24)} {hit.result.availability.label:<4} {marks} {note}{action}\n"
    )
    stream.flush()


async def _watch_async(bundle: Bundle, args: argparse.Namespace) -> int:
    settings = bundle.settings
    fetcher, engine = _build_runtime(bundle, reporter=_describe_hit)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    print(f"apple-sniper {__version__} · 开始监控 {len([t for t in engine.targets if t.enabled])} 个目标")
    print(
        f"  间隔 {settings.interval_seconds:g}s（抖动 ±{settings.jitter_ratio:.0%}）· "
        f"每主机最短 {settings.min_host_interval_seconds:g}s · 上限 {settings.max_requests_per_minute} 次/分"
    )
    print(f"  状态库 {settings.state_db}")
    print(f"  通知渠道 {'、'.join(c.kind for c in settings.channels)}")
    if settings.open_url_on_hit:
        print("  命中后自动打开商品页：开")
    if settings.command_on_hit:
        print(f"  命中后自动执行：{settings.command_on_hit}")
    print("  Ctrl-C 退出")
    print()

    try:
        stats = await engine.run(max_ticks=args.max_ticks, stop=stop)
    finally:
        await engine.notifier.aclose()
        await fetcher.aclose()

    print()
    print(
        f"结束：{stats.ticks} 轮 / {stats.checks} 次检查 / 抓取失败 {stats.failures} / "
        f"事件 {stats.events} / 到货 {stats.restocks}"
    )
    for note in stats.actions:
        print(f"  动作：{note}")
    return 1 if stats.failures and stats.checks == stats.failures else 0


def cmd_history(args: argparse.Namespace) -> int:
    bundle = load_bundle(args.config_dir)
    store = Store(bundle.settings.state_db)
    try:
        events = store.recent_events(args.limit, target_id=args.target)
        if not events:
            print("暂无事件记录")
            return 0
        rows = [
            [
                event["at"].replace("T", " ")[:19],
                event["target_id"],
                event["label"],
                event["message"],
            ]
            for event in events
        ]
        print_table(rows, ["时间(UTC)", "目标", "事件", "说明"])
        print()
        print("最近状态：")
        for target in bundle.targets:
            state = store.state(target.id)
            if not state:
                print(f"  {target.id}: 尚未检查过")
                continue
            stale = f"（已连续 {state['consecutive_errors']} 次抓取失败，数据可能陈旧）" if state["stale"] else ""
            print(
                f"  {target.id}: {state['availability_label']} 自 {state['since'][:19]}{stale}"
                f" / 最近检查 {state['last_checked'][:19]}"
            )
    finally:
        store.close()
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    bundle = load_bundle(args.config_dir)
    targets = _select_targets(bundle, args.target)
    store = Store(bundle.settings.state_db)
    try:
        forget(store, [t.id for t in targets])
    finally:
        store.close()
    names = "、".join(t.id for t in targets)
    print(f"已清空状态与历史：{names}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    bundle = load_bundle(args.config_dir)
    try:
        import uvicorn
    except ImportError:
        print("需要额外依赖：pip install 'apple-sniper[dashboard]'（即 fastapi + uvicorn）", file=sys.stderr)
        return 2
    from .server import create_app

    app = create_app(bundle)
    print(f"看板已启动：http://{args.host}:{args.port}  (Ctrl-C 退出)")
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apple-sniper",
        description="Apple 产品到货监控与抢购辅助（只做公开页面的低频轮询 + 通知/跳转）",
    )
    parser.add_argument("--config-dir", help="配置目录，默认 ./config（也可用 APPLE_SNIPER_CONFIG_DIR）")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别，默认 INFO",
    )
    parser.add_argument("--version", action="version", version=f"apple-sniper {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("targets", help="列出配置里的目标与运行参数").set_defaults(func=cmd_targets)

    probe = sub.add_parser("probe", help="抓一次并打印真实响应，用来核对 JSON/HTML 路径")
    probe.add_argument("target", nargs="*", help="目标 id（可多个；不填则全部）")
    probe.add_argument("--chars", type=int, default=600, help="打印正文的前 N 个字符，默认 600")
    probe.set_defaults(func=cmd_probe)

    check = sub.add_parser("check", help="跑一轮检查（落库 + 通知），适合 cron")
    check.add_argument("--target", action="append", help="只检查指定目标（可重复）")
    check.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    check.set_defaults(func=cmd_check)

    drill = sub.add_parser("drill", help="用真实渠道发一条合成的「已到货」，验证通知链路")
    drill.add_argument("--target", help="借用哪个目标的名称与深链（默认第一个启用的目标）")
    drill.add_argument("--act", action="store_true", help="同时演练命中动作（打开购买页 / command_on_hit）")
    drill.add_argument("--message", help="自定义演练文案")
    drill.set_defaults(func=cmd_drill)

    watch = sub.add_parser("watch", help="持续轮询，命中即通知")
    watch.add_argument("--max-ticks", type=int, default=None, help="最多跑 N 轮后退出（调试用）")
    watch.set_defaults(func=cmd_watch)

    history = sub.add_parser("history", help="查看事件历史与最新状态")
    history.add_argument("--target", help="只看某个目标")
    history.add_argument("-n", "--limit", type=int, default=30, help="最多显示 N 条事件，默认 30")
    history.set_defaults(func=cmd_history)

    forget_cmd = sub.add_parser("forget", help="清空指定目标的状态与历史")
    forget_cmd.add_argument("--target", action="append", required=True, help="目标 id（可重复）")
    forget_cmd.set_defaults(func=cmd_forget)

    serve = sub.add_parser("serve", help="启动本地只读看板")
    serve.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1（仅本机）")
    serve.add_argument("--port", type=int, default=8787, help="端口，默认 8787")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format=LOG_FORMAT, stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        print("已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
