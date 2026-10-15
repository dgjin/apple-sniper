#!/usr/bin/env bash
# 离线端到端演示：全程走 mock 来源，不联网、不打开浏览器、不需要任何账号。
# 依次触发：首检有货 → 无货 → 补货 → 降价 → 抓取异常 → 恢复，最后打印事件历史。
#
# 用法：bash scripts/demo.sh        （约 25 秒）
set -uo pipefail

cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
if [ ! -x "$PY" ]; then
  echo "找不到 $PY；请先：python3 -m venv .venv && .venv/bin/pip install httpx PyYAML" >&2
  exit 1
fi

CONFIG_DIR=demo
INVENTORY="$CONFIG_DIR/inventory.json"
LOG=tmp/demo-watch.log

INITIAL='{
  "demo-iphone": {"available": false, "price": 9999, "detail": "演示：初始无货"},
  "demo-airpods": {"available": true, "price": 1899, "detail": "演示：初始有货"}
}'

write_inventory() { printf '%s\n' "$1" > "$INVENTORY"; }

mkdir -p tmp
rm -f tmp/demo-state.db tmp/demo-state.db-wal tmp/demo-state.db-shm
write_inventory "$INITIAL"

restore() {
  write_inventory "$INITIAL"
  if [ -n "${WATCH_PID:-}" ]; then
    kill "$WATCH_PID" 2>/dev/null || true
    wait "$WATCH_PID" 2>/dev/null || true
  fi
}
trap restore EXIT

echo "== 1/7 启动监控（mock 来源，每 2 秒一轮，最多 9 轮）"
"$PY" -m apple_sniper --config-dir "$CONFIG_DIR" watch --max-ticks 9 >"$LOG" 2>&1 &
WATCH_PID=$!
sleep 3

echo "== 2/7 iPhone 补货 9999（AirPods 转为无货）"
write_inventory '{
  "demo-iphone": {"available": true, "price": 9999, "detail": "演示：刚刚补货"},
  "demo-airpods": {"available": false, "price": 1899, "detail": "演示：卖完了"}
}'
sleep 3

echo "== 3/7 iPhone 降价 9499（AirPods 重新有货）"
write_inventory '{
  "demo-iphone": {"available": true, "price": 9499, "detail": "演示：降价中"},
  "demo-airpods": {"available": true, "price": 1799, "detail": "演示：重新上架"}
}'
sleep 3

echo "== 4/7 iPhone 抓取失败（模拟接口 5xx）"
write_inventory '{
  "demo-iphone": {"error": "模拟接口 503"},
  "demo-airpods": {"available": true, "price": 1799, "detail": "演示：有货"}
}'
sleep 3

echo "   等待监控跑完剩余轮次…"
wait "$WATCH_PID" 2>/dev/null || true
WATCH_PID=""

# 恢复这一步刻意放在 watch 结束之后单独跑一轮：
# 避免 watch 的轮次与脚本 sleep 抢时间，导致「恢复」事件时有时无。
echo "== 5/7 iPhone 恢复正常，单独跑一轮 check 确认恢复"
write_inventory '{
  "demo-iphone": {"available": true, "price": 9499, "detail": "演示：恢复"},
  "demo-airpods": {"available": true, "price": 1799, "detail": "演示：有货"}
}'
"$PY" -m apple_sniper --config-dir "$CONFIG_DIR" check

echo
echo "== 6/7 监控实时输出（$LOG）"
cat "$LOG"

echo
echo "== 7/7 事件历史（落库结果）"
"$PY" -m apple_sniper --config-dir "$CONFIG_DIR" history -n 20

echo
echo "演示结束。状态库留在 tmp/demo-state.db，看板可用："
echo "  $PY -m apple_sniper --config-dir $CONFIG_DIR serve --port 8787"
