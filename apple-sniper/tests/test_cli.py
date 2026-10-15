"""CLI 子命令：drill（命中演练）。"""

from __future__ import annotations

from pathlib import Path

from helpers import run  # noqa: F401  （导入即把项目根塞进 sys.path）

from apple_sniper.cli import main

SETTINGS = """
notify_kinds:
  - back_in_stock
channels:
  - kind: console
"""

TARGETS = """
targets:
  - id: t1
    name: 测试目标
    kind: generic_json
    url: "https://shop.test/api?sku=A1"
    buy_url: "https://shop.test/buy/A1"
    match:
      path: data.stock
      when: "gt:0"
"""


def _config_dir(tmp_path: Path, settings: str = SETTINGS) -> Path:
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.yaml").write_text(settings, encoding="utf-8")
    (config / "targets.yaml").write_text(TARGETS, encoding="utf-8")
    return config


def test_drill_reports_each_channel(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config = _config_dir(tmp_path)

    code = main(["--config-dir", str(config), "drill"])

    out = capsys.readouterr().out
    assert code == 0
    assert "✓ channels[0] console" in out
    assert "渠道 1 个，失败 0 个" in out
    assert "测试目标" in out


def test_drill_flags_failing_channel(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config = _config_dir(
        tmp_path,
        SETTINGS + "  - kind: command\n    command: \"/usr/bin/false\"\n",
    )

    code = main(["--config-dir", str(config), "drill"])

    out = capsys.readouterr().out
    assert code == 1
    assert "✓ channels[0] console" in out
    assert "✗ channels[1] command" in out
    assert "渠道 2 个，失败 1 个" in out


def test_drill_warns_when_kind_not_subscribed(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config = _config_dir(tmp_path, "notify_kinds: [out_of_stock]\nchannels:\n  - kind: console\n")

    main(["--config-dir", str(config), "drill"])

    err = capsys.readouterr().err
    assert "notify_kinds" in err and "back_in_stock" in err
