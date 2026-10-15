#!/usr/bin/env bash
# 定时检查入口：抓一轮 → 落库 → 按需通知。供 launchd / crontab 调用。
#
# 为什么要包一层：定时器给的环境极简（cron 的 PATH 里只有 /usr/bin:/bin，也不读你的 shell 配置），
# 所以这里自己定位项目根、用绝对路径的虚拟环境 Python，并把输出追到日志里。
#
# macOS 用 launchd（推荐，能进 GUI 会话，`open` 打开浏览器才有效）：
#   见 README「定时跑」；装好 plist 后由 launchd 每 60s 调用本脚本。
# Linux / 已给 cron 授权时：
#   crontab -e
#   * * * * * /绝对路径/apple-sniper/scripts/cron-check.sh
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
PROJECT_DIR="$(pwd)"
PY="${PY:-$PROJECT_DIR/.venv/bin/python}"

if [ ! -x "$PY" ]; then
  echo "$(date '+%F %T') 找不到虚拟环境 Python：$PY（先跑 python3 -m venv .venv）" >&2
  exit 1
fi

LOG_DIR="$PROJECT_DIR/data"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/check.log"

# 同一时刻只允许一轮：网络卡住时上一轮可能还没跑完（最坏约 超时×重试 ≈ 30s）。
LOCK="$LOG_DIR/.cron-check.lock"
LOCK_PID="$LOCK/pid"

# 陈旧锁清理：进程被强杀（launchd bootout、SIGKILL）时锁会留在原地——bash 要等前台
# 子进程结束才执行 EXIT trap，所以强杀时 trap 往往来不及跑。纯按 mtime 判断最长会「盲」
# 5 分钟，而窗口期切换轮询频率正好会踩到。改为以「锁里的 PID 是否还活着」为准：
# 持有者已死 → 立刻清；PID 被复用的极端情况用 5 分钟上限兜底。
if [ -d "$LOCK" ]; then
  holder="$(cat "$LOCK_PID" 2>/dev/null || true)"
  if [ -z "$holder" ]; then
    # 旧格式锁 / pid 文件写入中断：退回按时间判断，避免误清正在跑的锁
    if [ -n "$(find "$LOCK" -mmin +5 2>/dev/null)" ]; then
      rmdir "$LOCK" 2>/dev/null || true
    fi
  elif ! kill -0 "$holder" 2>/dev/null || [ -n "$(find "$LOCK" -mmin +5 2>/dev/null)" ]; then
    rm -f "$LOCK_PID" 2>/dev/null || true
    rmdir "$LOCK" 2>/dev/null || true
  fi
fi
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date '+%F %T') 上一轮仍在运行（pid $(cat "$LOCK_PID" 2>/dev/null || echo '?')），跳过本次" >> "$LOG"
  exit 0
fi
echo $$ > "$LOCK_PID"
trap 'rm -f "$LOCK_PID" 2>/dev/null || true; rmdir "$LOCK" 2>/dev/null || true' EXIT INT TERM

# 日志轮转：超过阈值就只保留末尾若干行，避免 check.log 无上限增长。
# 阈值 / 保留行数可用环境变量覆盖（plist 里可加 EnvironmentVariables）。
MAX_LOG_BYTES="${APPLE_SNIPER_LOG_MAX_BYTES:-1048576}"
KEEP_LOG_LINES="${APPLE_SNIPER_LOG_KEEP_LINES:-2000}"
if [ -f "$LOG" ]; then
  size="$(wc -c < "$LOG" | tr -d ' ')"
  if [ "${size:-0}" -gt "$MAX_LOG_BYTES" ] 2>/dev/null; then
    tail -n "$KEEP_LOG_LINES" "$LOG" > "$LOG.tmp"
    echo "----- $(date '+%F %T') 日志已轮转：原 ${size} 字节，保留末尾 ${KEEP_LOG_LINES} 行 -----" >> "$LOG.tmp"
    mv "$LOG.tmp" "$LOG"
  fi
fi

{
  echo "----- $(date '+%F %T') check 开始 -----"
  "$PY" -m apple_sniper --config-dir "$PROJECT_DIR/config" check
  code=$?
  echo "----- $(date '+%F %T') check 结束（退出码 $code）-----"
} >> "$LOG" 2>&1

exit "$code"
