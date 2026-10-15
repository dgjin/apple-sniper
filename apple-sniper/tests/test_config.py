"""配置加载、校验与路径解析。"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import clean_env

from apple_sniper.config import ConfigError, load_bundle, load_settings, load_targets
from apple_sniper.models import EventKind

MINIMAL_SETTINGS = "channels:\n  - kind: console\n"

MINIMAL_TARGETS = """
targets:
  - id: t1
    name: 一号
    kind: generic_json
    url: "https://shop.test/api?sku={sku}"
    match:
      params: {sku: "A1"}
      path: data.stock
      when: "gt:0"
"""


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class TestLoadSettings:
    def test_defaults_applied(self, tmp_path: Path) -> None:
        settings = load_settings(write(tmp_path / "settings.yaml", MINIMAL_SETTINGS), base_dir=tmp_path)
        assert settings.interval_seconds == 30.0
        assert settings.renotify_cooldown_seconds == 900.0
        assert settings.retries == 2
        assert settings.open_url_on_hit is False
        assert settings.escalate_repeats == 1
        assert settings.escalate_interval_seconds == 5.0
        assert [c.kind for c in settings.channels] == ["console"]
        assert Path(settings.state_db).is_absolute()
        assert Path(settings.state_db).parent == tmp_path / "data"

    def test_explicit_values_and_options(self, tmp_path: Path) -> None:
        text = """
interval_seconds: 15
jitter_ratio: 0
min_host_interval_seconds: 5
max_requests_per_minute: 6
retries: 0
open_url_on_hit: "yes"
command_on_hit: "/bin/echo hi"
escalate_repeats: 3
escalate_interval_seconds: 2
notify_kinds: [back_in_stock, price_drop]
channels:
  - kind: macos
    title: 自定义标题
    sound: default
"""
        settings = load_settings(write(tmp_path / "settings.yaml", text), base_dir=tmp_path)
        assert settings.interval_seconds == 15.0
        assert settings.jitter_ratio == 0.0
        assert settings.min_host_interval_seconds == 5.0
        assert settings.max_requests_per_minute == 6
        assert settings.open_url_on_hit is True
        assert settings.command_on_hit == "/bin/echo hi"
        assert settings.escalate_repeats == 3
        assert settings.escalate_interval_seconds == 2.0
        assert [k.value for k in settings.notify_kinds] == ["back_in_stock", "price_drop"]
        assert settings.channels[0].options == {"title": "自定义标题", "sound": "default"}

    def test_bad_number_reports_field(self, tmp_path: Path) -> None:
        text = "interval_seconds: 快\nchannels:\n  - kind: console\n"
        with pytest.raises(ConfigError, match="interval_seconds 需要数字"):
            load_settings(write(tmp_path / "settings.yaml", text), base_dir=tmp_path)

    def test_missing_channels_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="缺少 channels"):
            load_settings(write(tmp_path / "settings.yaml", "interval_seconds: 5\n"), base_dir=tmp_path)

    def test_unknown_channel_rejected(self, tmp_path: Path) -> None:
        text = "channels:\n  - kind: telegram\n"
        with pytest.raises(ConfigError, match="kind='telegram' 不支持"):
            load_settings(write(tmp_path / "settings.yaml", text), base_dir=tmp_path)

    def test_unknown_notify_kind_rejected(self, tmp_path: Path) -> None:
        text = MINIMAL_SETTINGS + "notify_kinds: [back_in_stock, 到货]\n"
        with pytest.raises(ConfigError, match="含未知事件"):
            load_settings(write(tmp_path / "settings.yaml", text), base_dir=tmp_path)

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="找不到配置文件"):
            load_settings(tmp_path / "nope.yaml", base_dir=tmp_path)

    def test_non_mapping_top_level(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="顶层需要字典"):
            load_settings(write(tmp_path / "settings.yaml", "- a\n- b\n"), base_dir=tmp_path)

    def test_env_override_wins(self, tmp_path: Path) -> None:
        with clean_env(APPLE_SNIPER_INTERVAL_SECONDS="5", APPLE_SNIPER_STATE_DB="custom.db"):
            settings = load_settings(
                write(tmp_path / "settings.yaml", MINIMAL_SETTINGS), base_dir=tmp_path
            )
        assert settings.interval_seconds == 5.0
        assert settings.state_db == str(tmp_path / "custom.db")

    def test_env_bad_value_raises(self, tmp_path: Path) -> None:
        with clean_env(APPLE_SNIPER_RETRIES="many"):
            with pytest.raises(ConfigError, match="不是合法 int"):
                load_settings(write(tmp_path / "settings.yaml", MINIMAL_SETTINGS), base_dir=tmp_path)

    def test_escalate_env_override(self, tmp_path: Path) -> None:
        with clean_env(APPLE_SNIPER_ESCALATE_REPEATS="4", APPLE_SNIPER_ESCALATE_INTERVAL_SECONDS="1.5"):
            settings = load_settings(
                write(tmp_path / "settings.yaml", MINIMAL_SETTINGS), base_dir=tmp_path
            )
        assert settings.escalate_repeats == 4
        assert settings.escalate_interval_seconds == 1.5

    def test_http_proxy_defaults_to_none(self, tmp_path: Path) -> None:
        with clean_env():
            settings = load_settings(
                write(tmp_path / "settings.yaml", MINIMAL_SETTINGS), base_dir=tmp_path
            )
        assert settings.http_proxy is None

    def test_http_proxy_from_yaml_and_env(self, tmp_path: Path) -> None:
        text = MINIMAL_SETTINGS + 'http_proxy: "http://127.0.0.1:7891"\n'
        settings = load_settings(write(tmp_path / "settings.yaml", text), base_dir=tmp_path)
        assert settings.http_proxy == "http://127.0.0.1:7891"

        with clean_env(APPLE_SNIPER_HTTP_PROXY="http://127.0.0.1:7890"):
            settings = load_settings(
                write(tmp_path / "settings.yaml", MINIMAL_SETTINGS), base_dir=tmp_path
            )
        assert settings.http_proxy == "http://127.0.0.1:7890"

    def test_notify_heartbeat_defaults_to_off(self, tmp_path: Path) -> None:
        with clean_env():
            settings = load_settings(
                write(tmp_path / "settings.yaml", MINIMAL_SETTINGS), base_dir=tmp_path
            )
        assert settings.notify_heartbeat_seconds == 0.0

    def test_notify_heartbeat_from_yaml_and_env(self, tmp_path: Path) -> None:
        text = MINIMAL_SETTINGS + "notify_heartbeat_seconds: 43200\n"
        settings = load_settings(write(tmp_path / "settings.yaml", text), base_dir=tmp_path)
        assert settings.notify_heartbeat_seconds == 43200.0

        with clean_env(APPLE_SNIPER_NOTIFY_HEARTBEAT_SECONDS="60"):
            settings = load_settings(
                write(tmp_path / "settings.yaml", MINIMAL_SETTINGS), base_dir=tmp_path
            )
        assert settings.notify_heartbeat_seconds == 60.0

    def test_heartbeat_is_a_valid_notify_kind(self, tmp_path: Path) -> None:
        text = MINIMAL_SETTINGS + "notify_kinds: [back_in_stock, heartbeat]\n"
        settings = load_settings(write(tmp_path / "settings.yaml", text), base_dir=tmp_path)
        assert settings.notify_kinds == (EventKind.BACK_IN_STOCK, EventKind.HEARTBEAT)


class TestLoadTargets:
    def test_renders_url_and_keeps_match(self, tmp_path: Path) -> None:
        targets = load_targets(write(tmp_path / "targets.yaml", MINIMAL_TARGETS), base_dir=tmp_path)
        assert len(targets) == 1
        target = targets[0]
        assert target.url == "https://shop.test/api?sku=A1"
        assert target.enabled is True
        assert target.match["when"] == "gt:0"

    def test_bare_list_is_accepted(self, tmp_path: Path) -> None:
        text = "- id: t1\n  kind: mock\n"
        targets = load_targets(write(tmp_path / "targets.yaml", text), base_dir=tmp_path)
        assert targets[0].id == "t1"
        # mock 源不联网：没写 url 时给一个可读的占位，并把库存文件解析成绝对路径。
        assert targets[0].url == "mock://t1"
        assert Path(targets[0].match["inventory_file"]).is_absolute()

    def test_duplicate_id_rejected(self, tmp_path: Path) -> None:
        text = "targets:\n  - {id: t1, kind: mock}\n  - {id: t1, kind: mock}\n"
        with pytest.raises(ConfigError, match="id 重复"):
            load_targets(write(tmp_path / "targets.yaml", text), base_dir=tmp_path)

    def test_unknown_kind_rejected(self, tmp_path: Path) -> None:
        text = "targets:\n  - {id: t1, kind: rss, url: 'https://x.test'}\n"
        with pytest.raises(ConfigError, match="kind='rss' 不支持"):
            load_targets(write(tmp_path / "targets.yaml", text), base_dir=tmp_path)

    def test_missing_id_rejected(self, tmp_path: Path) -> None:
        text = "targets:\n  - {kind: mock}\n"
        with pytest.raises(ConfigError, match="缺少 id"):
            load_targets(write(tmp_path / "targets.yaml", text), base_dir=tmp_path)

    def test_missing_url_rejected(self, tmp_path: Path) -> None:
        text = "targets:\n  - {id: t1, kind: generic_json}\n"
        with pytest.raises(ConfigError, match="缺少 url"):
            load_targets(write(tmp_path / "targets.yaml", text), base_dir=tmp_path)

    def test_url_template_missing_param_rejected(self, tmp_path: Path) -> None:
        text = 'targets:\n  - {id: t1, kind: generic_json, url: "https://x.test/{sku}"}\n'
        with pytest.raises(ConfigError, match="缺少参数"):
            load_targets(write(tmp_path / "targets.yaml", text), base_dir=tmp_path)

    def test_per_target_interval(self, tmp_path: Path) -> None:
        text = "targets:\n  - {id: t1, kind: mock, interval_seconds: 7}\n"
        targets = load_targets(write(tmp_path / "targets.yaml", text), base_dir=tmp_path)
        assert targets[0].interval_seconds == 7.0
        assert targets[0].effective_interval(30.0) == 7.0

    def test_renders_buy_url_deep_link(self, tmp_path: Path) -> None:
        """buy_url 也用同一套 {参数} 模板，用来拼「预选型号」的直达购买链接。"""
        text = """
targets:
  - id: duo
    kind: generic_json
    url: "https://shop.test/api?sku={sku}"
    buy_url: "https://www.apple.com.cn/shop/buy-iphone/iphone-duo?color={color}&capacity={capacity}"
    match:
      params: {sku: A1, color: 深空灰, capacity: 1TB}
      path: data.stock
      when: "gt:0"
"""
        target = load_targets(write(tmp_path / "targets.yaml", text), base_dir=tmp_path)[0]
        assert target.buy_url == (
            "https://www.apple.com.cn/shop/buy-iphone/iphone-duo?color=深空灰&capacity=1TB"
        )
        # 命中动作与通知里的链接都走 action_url ⇒ 优先用直达链接。
        assert target.action_url == target.buy_url

    def test_buy_url_missing_param_rejected(self, tmp_path: Path) -> None:
        text = 'targets:\n  - {id: t1, kind: mock, buy_url: "https://x.test/{color}"}\n'
        with pytest.raises(ConfigError, match="buy_url 模板 缺少参数"):
            load_targets(write(tmp_path / "targets.yaml", text), base_dir=tmp_path)

    def test_notify_accepts_single_string(self, tmp_path: Path) -> None:
        text = "targets:\n  - {id: t1, kind: mock, notify: bark}\n"
        targets = load_targets(write(tmp_path / "targets.yaml", text), base_dir=tmp_path)
        assert targets[0].notify == ("bark",)


class TestLoadBundle:
    def test_relative_paths_resolve_against_project_root(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        write(config_dir / "settings.yaml", MINIMAL_SETTINGS + "state_db: data/x.db\n")
        write(config_dir / "targets.yaml", MINIMAL_TARGETS)

        bundle = load_bundle(config_dir)
        assert bundle.config_dir == config_dir
        # state_db 相对路径按「配置目录的父级」= 项目根解析，不受运行目录影响。
        assert Path(bundle.settings.state_db) == tmp_path / "data" / "x.db"
        assert bundle.targets[0].id == "t1"

    def test_missing_targets_file(self, tmp_path: Path) -> None:
        write(tmp_path / "settings.yaml", MINIMAL_SETTINGS)
        with pytest.raises(ConfigError, match="targets.yaml"):
            load_bundle(tmp_path)
