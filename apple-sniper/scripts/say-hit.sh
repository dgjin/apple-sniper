#!/usr/bin/env bash
# 命中语音播报：念出「哪个门店 · 已到货」，人不在屏幕前也能听见。
#
# 由 `command_on_hit` 调用（CommandChannel）：事件字段通过 APPLE_SNIPER_* 环境变量传入，
# 不经过 shell，页面内容不会被拼进命令行。配置示例（用绝对路径最稳）：
#   command_on_hit: "/绝对路径/apple-sniper/scripts/say-hit.sh"
#
# 可用 APPLE_SNIPER_VOICE 换音色（默认 Tingting，macOS 自带中文女声；
# 不装中文语音包时 say 会回退到默认音色而不是报错）。
set -uo pipefail

VOICE="${APPLE_SNIPER_VOICE:-Tingting}"
name="${APPLE_SNIPER_TARGET_NAME:-apple-sniper}"
msg="${APPLE_SNIPER_MESSAGE:-已到货}"

# 后台念：say 会读完整句才退出，别让它拖住命中动作与升级提醒。
nohup /usr/bin/say -v "$VOICE" "${name}，${msg}" >/dev/null 2>&1 &
exit 0
