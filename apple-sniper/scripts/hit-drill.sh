#!/usr/bin/env bash
# 命中演练：用**真实**通知渠道发一条合成的「已到货」，确认手机 / 桌面 / 语音链路都通。
# 不抓取页面、不下单。加 --act 才会真的打开购买页并执行 command_on_hit。
#
# 用法：
#   bash scripts/hit-drill.sh                 # 逐渠道验证通知（不发真货）
#   bash scripts/hit-drill.sh --act           # 连命中动作（打开购买页 + 语音）一起验
#   bash scripts/hit-drill.sh --target iphone-duo-xidan --act
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
PY="${PY:-$(pwd)/.venv/bin/python}"
if [ ! -x "$PY" ]; then
  echo "找不到虚拟环境 Python：$PY（先跑 python3 -m venv .venv）" >&2
  exit 1
fi

exec "$PY" -m apple_sniper --config-dir config drill "$@"
