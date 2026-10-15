#!/usr/bin/env bash
# 预购/发售窗口期「一键切模式」：进窗口收紧轮询 + 防睡眠；出窗口恢复常态。
#
# 为什么需要它（都是实测踩过的坑）：
#   1. launchd 的 StartInterval 是「两轮之间的**空闲**时间」，不是轮次周期——
#      实际周期 ≈ StartInterval + 单轮耗时（6 店单轮约 18s）。所以「想 ~20s 一轮」要填 2，不是 20。
#      填 20 只会得到 ~38s。
#   2. `launchctl bootout` 是**异步**的：紧接着 bootstrap 会**静默失败**，把监控整个搞丢。
#      本脚本按「停调度 → 等旧 job 注销 → 清残留锁 → 落新 plist → bootstrap → 校验」的顺序做。
#   3. 强杀跑到一半的轮次会留下防重入锁；这里在装载前主动等它退出并清掉，避免「盲」。
#   4. 切完必须**确认新轮次真的跑起来了**——plist 里写对不代表监控在跑。
#
# 用法：
#   scripts/window-mode.sh on           # 进窗口：StartInterval 2（≈20s 周期）+ caffeinate 防睡眠
#   scripts/window-mode.sh off          # 出窗口：恢复 StartInterval 60，停 caffeinate
#   scripts/window-mode.sh status       # 当前模式 / 已装载与仓库的间隔 / job / caffeinate / 最近轮次
#
# 可调：
#   WINDOW_INTERVAL=3 scripts/window-mode.sh on       # 周期想松一点（≈21s）
#   WINDOW_MODE_NO_WAIT=1 scripts/window-mode.sh on   # 切完不等轮次确认
#
# 注意：
#   - 仓库里的 plist 永远是「常态」基准，本脚本**不改它**；只改 ~/Library/LaunchAgents 里装载的那份。
#   - `on` 会启动**无限期**的 caffeinate（防系统/合盖睡眠）并写 data/.window-mode 状态；窗口结束
#     务必跑 `off`，否则会一直 20s 轮询 + 阻止睡眠。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

UID_NUM="$(id -u)"
LABEL="com.dgjin.apple-sniper.check"
DOMAIN="gui/${UID_NUM}"
REPO_PLIST="$PWD/scripts/com.dgjin.apple-sniper.check.plist"
INSTALLED_PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG="$PWD/data/check.log"
LOCK="$PWD/data/.cron-check.lock"
CAF_PID_FILE="$PWD/data/.window-caffeinate.pid"
STATE_FILE="$PWD/data/.window-mode"

read_interval() {  # 从 plist 取 StartInterval
  awk '/<key>StartInterval<\/key>/{getline; gsub(/[^0-9]/,""); print}' "$1" 2>/dev/null
}

WINDOW_INTERVAL="${WINDOW_INTERVAL:-2}"
case "$WINDOW_INTERVAL" in ''|*[!0-9]*) echo "✗ WINDOW_INTERVAL 必须是整数：$WINDOW_INTERVAL" >&2; exit 2;; esac
NORMAL_INTERVAL="$(read_interval "$REPO_PLIST")"
NORMAL_INTERVAL="${NORMAL_INTERVAL:-60}"
WAIT=1; [ "${WINDOW_MODE_NO_WAIT:-0}" = "1" ] && WAIT=0

die() { echo "✗ $*" >&2; exit 1; }
job_loaded() { launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; }
caf_pid() { cat "$CAF_PID_FILE" 2>/dev/null || true; }
caf_alive() { local p; p="$(caf_pid)"; [ -n "$p" ] && kill -0 "$p" 2>/dev/null; }

# 停调度：bootout（异步）→ 等旧 job 注销 → 等跑到一半的轮次退出 → 清它留下的锁。
stop_job() {
  local i
  launchctl bootout "${DOMAIN}/${LABEL}" >/dev/null 2>&1 || true
  for i in $(seq 1 40); do job_loaded || break; sleep 0.5; done
  job_loaded && die "旧 job 20s 内未注销，放弃切换（监控仍在，未做改动）"
  for i in $(seq 1 40); do
    pgrep -f "apple_sniper .*check" >/dev/null 2>&1 || break
    sleep 1
  done
  if [ -d "$LOCK" ]; then
    rm -f "$LOCK/pid" 2>/dev/null || true
    rmdir "$LOCK" 2>/dev/null && echo "  已清掉上一轮强杀残留的防重入锁"
  fi
}

start_job() {
  local plist="$1" i
  launchctl bootstrap "$DOMAIN" "$plist" || die "bootstrap 失败：$plist"
  for i in $(seq 1 40); do job_loaded && break; sleep 0.5; done
  job_loaded || die "装载后校验失败：$plist"
}

# 唯一能证明「监控没瞎」的办法：看到新的 check 开始。
confirm_running() {
  local budget="$1" before now waited=0
  before="$(grep -c 'check 开始' "$LOG" 2>/dev/null || echo 0)"
  while [ "$waited" -lt "$budget" ]; do
    sleep 5; waited=$((waited + 5))
    now="$(grep -c 'check 开始' "$LOG" 2>/dev/null || echo 0)"
    if [ "$now" -gt "$before" ]; then
      echo "  ✓ 已确认新轮次：$(grep 'check 开始' "$LOG" | tail -1 | sed 's/^----- //; s/ -----$//')"
      return 0
    fi
  done
  echo "  ！${budget}s 内没看到新轮次——请手动确认：tail -f data/check.log" >&2
  return 1
}

require_installed() {
  [ -f "$INSTALLED_PLIST" ] || die "未安装 LaunchAgent：$INSTALLED_PLIST
  先执行： cp scripts/${LABEL}.plist ~/Library/LaunchAgents/ && launchctl bootstrap ${DOMAIN} ~/Library/LaunchAgents/${LABEL}.plist"
}

cmd_on() {
  require_installed
  local tmp="$PWD/tmp/window-${LABEL}.plist"
  mkdir -p tmp
  sed "s|<integer>${NORMAL_INTERVAL}</integer>|<integer>${WINDOW_INTERVAL}</integer>|" "$REPO_PLIST" > "$tmp"
  [ "$(read_interval "$tmp")" = "$WINDOW_INTERVAL" ] || die "生成窗口 plist 失败（间隔未被替换）"

  echo "进窗口：StartInterval ${NORMAL_INTERVAL} → ${WINDOW_INTERVAL}（实际周期 ≈ ${WINDOW_INTERVAL}s + 单轮耗时 ≈ $((WINDOW_INTERVAL + 18))s）"
  stop_job
  cp "$tmp" "$INSTALLED_PLIST"
  start_job "$INSTALLED_PLIST"
  echo "  已装载 StartInterval=$(read_interval "$INSTALLED_PLIST")"

  if caf_alive; then
    echo "  caffeinate 已在运行（pid $(caf_pid)）"
  else
    nohup caffeinate -dims >/dev/null 2>&1 &
    echo $! > "$CAF_PID_FILE"
    echo "  已启动 caffeinate -dims（pid $(cat "$CAF_PID_FILE")）：阻止系统/合盖睡眠"
  fi
  date '+%F %T' > "$STATE_FILE"
  [ "$WAIT" = 1 ] && confirm_running 40
  echo "✓ 已进入窗口模式。窗口结束后记得：scripts/window-mode.sh off"
}

cmd_off() {
  require_installed
  echo "出窗口：恢复 StartInterval ${NORMAL_INTERVAL}"
  stop_job
  cp "$REPO_PLIST" "$INSTALLED_PLIST"
  start_job "$INSTALLED_PLIST"
  echo "  已装载 StartInterval=$(read_interval "$INSTALLED_PLIST")"

  if caf_alive; then
    kill "$(caf_pid)" 2>/dev/null && echo "  已停 caffeinate（pid $(caf_pid)）"
  else
    echo "  caffeinate 未在运行"
  fi
  rm -f "$CAF_PID_FILE" "$STATE_FILE"
  [ "$WAIT" = 1 ] && confirm_running 40
  echo "✓ 已恢复常态。"
}

cmd_status() {
  local repo_int inst_int mode
  repo_int="$(read_interval "$REPO_PLIST")"
  inst_int="$([ -f "$INSTALLED_PLIST" ] && read_interval "$INSTALLED_PLIST" || echo '未安装')"
  if [ -f "$STATE_FILE" ]; then
    mode="窗口模式（自 $(cat "$STATE_FILE") 起）"
  else
    mode="常态"
  fi
  echo "模式          : $mode"
  echo "仓库 plist 间隔: ${repo_int:-?}   （常态基准）"
  echo "已装载 间隔    : ${inst_int:-?}"
  echo "job 已加载     : $(job_loaded && echo 是 || echo '否 ← 监控没在跑！')"
  echo "caffeinate     : $(caf_alive && echo "运行中 pid $(caf_pid)" || echo 未运行)"
  echo "最近轮次       : $(grep 'check 开始' "$LOG" 2>/dev/null | tail -1 | sed 's/^----- //; s/ -----$//' || echo 无)"
  echo "最后一次跳过   : $(grep '跳过本次' "$LOG" 2>/dev/null | tail -1 || echo 无)"
  if [ -d "$LOCK" ]; then
    local holder; holder="$(cat "$LOCK/pid" 2>/dev/null || true)"
    if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
      echo "防重入锁       : 持有中（pid $holder，轮次进行中，正常）"
    else
      echo "防重入锁       : 陈旧残留（持有者已退出，下一轮会自动清）"
    fi
  else
    echo "防重入锁       : 无"
  fi
  if [ "${inst_int:-}" != "${repo_int:-}" ]; then
    echo "！已装载间隔与仓库基准不一致——正常只在窗口期如此；窗口结束请跑 off。"
  fi
}

usage() {
  cat <<'EOF'
用法：
  scripts/window-mode.sh on      进窗口：StartInterval 2（≈20s 周期）+ caffeinate 防睡眠
  scripts/window-mode.sh off     出窗口：恢复 StartInterval 60，停 caffeinate
  scripts/window-mode.sh status  看当前模式 / 已装载与仓库的间隔 / job / caffeinate / 最近轮次

可调：
  WINDOW_INTERVAL=3 scripts/window-mode.sh on        周期松一点（≈21s）
  WINDOW_MODE_NO_WAIT=1 scripts/window-mode.sh on    切完不等轮次确认

注意：仓库 plist 是「常态」基准，本脚本不改它；`on` 会启动无限期 caffeinate，窗口结束务必 `off`。
EOF
}

case "${1:-}" in
  on)     cmd_on ;;
  off)    cmd_off ;;
  status) cmd_status ;;
  *)      usage; exit 2 ;;
esac
