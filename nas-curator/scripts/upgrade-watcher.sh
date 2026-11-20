#!/bin/bash
# scripts/upgrade-watcher.sh —— NAS 侧升级 watcher（0.1.4）。
#
# 由 systemd timer 每分钟触发一次：检查数据卷上的升级请求标志文件
# /vol2/@appshare/nas-curator/data/upgrade/request.json；
# 有 status=requested 的请求时，调用同目录的 upgrade.sh 执行升级。
# 本脚本只做触发与锁判断，不含升级逻辑；升级进度由 upgrade.sh 写状态文件。
set -uo pipefail

UPGRADE_DIR="/vol2/@appshare/nas-curator/data/upgrade"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] upgrade-watcher: $*"; }

req="$UPGRADE_DIR/request.json"
[ -f "$req" ] || exit 0  # 无请求，直接退出

status=$(python3 -c "import json; print(json.load(open('$req')).get('status',''))" 2>/dev/null || echo "")
[ "$status" = "requested" ] || exit 0  # 不是待处理请求，退出

# 升级进行中（锁被占）时跳过，等下一分钟
exec 9>"$UPGRADE_DIR/lock"
if ! flock -n 9; then
  log "升级进行中，跳过本次触发"
  exit 0
fi
exec 9>&-  # 放锁，交给 upgrade.sh 重新拿锁执行

target=$(python3 -c "import json; print(json.load(open('$req')).get('target_version',''))" 2>/dev/null || echo "")
log "拾取升级请求 → $target，执行 upgrade.sh"
exec "$SCRIPT_DIR/upgrade.sh"
