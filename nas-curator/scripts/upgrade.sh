#!/bin/bash
# scripts/upgrade.sh —— NAS 侧升级执行脚本（0.1.4）。
#
# 由 systemd timer 每分钟跑的 upgrade-watcher.sh 触发；绝不在容器内运行
# （容器不能重启自己，也不挂 docker socket）。
# 流程：读请求 → 取代码（VM 同步暂存优先，git 拉取作后备）→ 构建新镜像 →
#       备份数据库 → 换 compose 重建 → 验证（healthy + 六页 200 + 链顺序与
#       OCR 探针）→ 任一步失败则用上一版镜像重建回滚。升级只在他点「一键升级」
#       按钮时触发，绝不自动执行。
#
# 用法：upgrade.sh [--dry-run]
#   --dry-run 只做到构建镜像成功为止（拉代码 + 构建），不动容器、不动 DB、
#   不换 compose；成功后状态文件写 dry-run-ok。用于验证升级路径。
#
# 运行身份：root（systemd service）；手动跑时自动补 sudo。
# 日志：stdout（systemd journal）+ 进度写 $DATA_DIR/upgrade/status.json。
set -uo pipefail

REPO="https://github.com/bjornwz-cn/muse.git"
DATA_DIR="/vol2/@appshare/nas-curator/data"
UPGRADE_DIR="$DATA_DIR/upgrade"
WORKDIR="/vol2/@appshare/nas-curator/upgrade-work"
APPDIR="/vol2/@appcenter/nas-curator/docker"
IMAGE_PREFIX="nas-curator"
PORT="8765"

if [ "$(id -u)" = "0" ]; then DOCKER="docker"; else DOCKER="sudo docker"; fi

DRY_RUN="false"
[ "${1:-}" = "--dry-run" ] && DRY_RUN="true"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] upgrade: $*"; }

# status_write <state> <stage> <message> [error] [rolled_back]
status_write() {
  python3 - "$UPGRADE_DIR/status.json" "$TARGET" "$CURRENT" "$STARTED_AT" \
    "$1" "$2" "$3" "${4:-}" "${5:-false}" <<'PYEOF'
import json, os, sys, time
path, target, current, started, state, stage, message, error, rolled = sys.argv[1:10]
data = {}
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    pass
finished = ""
if state in ("done", "failed", "dry-run-ok"):
    finished = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
data.update({"state": state, "stage": stage, "message": message,
             "target_version": target, "current_version": current,
             "started_at": started, "finished_at": finished,
             "error": error, "rolled_back": rolled == "true"})
os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False)
os.replace(tmp, path)
PYEOF
}

# fail <stage> <message>：写失败状态；若已动过容器则回滚；退出 1。
fail() {
  local stage="$1" msg="$2"
  log "FAILED [$stage]: $msg"
  if [ "$DEPLOYED" = "true" ]; then
    rollback "$msg"
  else
    status_write "failed" "$stage" "$msg" "$msg" "false"
  fi
  rm -f "$UPGRADE_DIR/request.json"
  exit 1
}

rollback() {
  local reason="$1"
  log "回滚：恢复 $OLD 的 compose 与镜像（原因：$reason）"
  status_write "failed" "rollback" "升级失败，正在回滚到 $OLD：$reason" "$reason" "false"
  local ok="true"
  if [ -f "$APPDIR/docker-compose.yaml.$OLD-bak" ]; then
    cp "$APPDIR/docker-compose.yaml.$OLD-bak" "$APPDIR/docker-compose.yaml" \
      || ok="false"
  else
    ok="false"
  fi
  if [ "$ok" = "true" ]; then
    (cd "$APPDIR" && TRIM_SERVICE_PORT=$PORT TRIM_APPDEST_VOL=vol2 \
      TRIM_APPNAME=nas-curator $DOCKER compose -p nas-curator up -d --force-recreate \
      >> "$UPGRADE_DIR/upgrade-$TARGET.log" 2>&1) || ok="false"
  fi
  if [ "$ok" = "true" ] && verify_running "$IMAGE_PREFIX:$OLD" "120"; then
    log "回滚成功：已回到 $OLD"
    status_write "failed" "rollback" "升级失败，已回滚到 $OLD" "$reason" "true"
  else
    log "回滚失败：需手动处理（原因：$reason）"
    status_write "failed" "rollback" "升级失败且自动回滚失败，请手动处理" \
      "$reason（回滚也失败）" "false"
  fi
}

# verify_running <image> <wait_seconds>：等 healthy + 六页与 dashboard 均 200 +
# settings 含 effective_order 且 OCR 可达。成功返回 0。
verify_running() {
  local want_image="$1" wait_s="${2:-300}" healthy=""
  local i
  for i in $(seq 1 $((wait_s / 5))); do
    healthy=$($DOCKER inspect -f '{{.State.Health.Status}}' nas-curator 2>/dev/null || echo "")
    [ "$healthy" = "healthy" ] && break
    sleep 5
  done
  [ "$healthy" = "healthy" ] || { log "verify: 容器未 healthy（当前：$healthy）"; return 1; }
  local img
  img=$($DOCKER inspect -f '{{.Config.Image}}' nas-curator 2>/dev/null || echo "")
  [ "$img" = "$want_image" ] || { log "verify: 镜像不符（当前 $img，期望 $want_image）"; return 1; }
  local p code
  for p in / /review /jobs /browse /rules /settings /api/dashboard; do
    code=$(curl -s -m 10 -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT$p" || echo "000")
    [ "$code" = "200" ] || { log "verify: $p 返回 $code（期望 200）"; return 1; }
  done
  curl -s -m 10 "http://127.0.0.1:$PORT/api/settings" | python3 -c \
    "import json,sys; d=json.load(sys.stdin); assert 'effective_order' in d, '无 effective_order'; assert d.get('ocr_status',{}).get('reachable') is True, 'OCR 不可达'" \
    || { log "verify: /api/settings 校验失败（缺 effective_order 或 OCR 不可达）"; return 1; }
  return 0
}

main() {
  mkdir -p "$UPGRADE_DIR" "$WORKDIR"
  exec 9>"$UPGRADE_DIR/lock"
  if ! flock -n 9; then
    log "已有升级在进行中（拿不到锁），本次退出"
    exit 0
  fi

  local req="$UPGRADE_DIR/request.json"
  [ -f "$req" ] || { log "无升级请求，退出"; exit 0; }
  TARGET=$(python3 -c "import json; print(json.load(open('$req')).get('target_version',''))")
  [ -n "$TARGET" ] || { log "请求文件无 target_version，退出"; exit 0; }
  local req_status
  req_status=$(python3 -c "import json; print(json.load(open('$req')).get('status',''))")
  [ "$req_status" = "requested" ] || { log "请求状态为 $req_status，非 requested，退出"; exit 0; }

  CURRENT=$($DOCKER inspect -f '{{.Config.Image}}' nas-curator 2>/dev/null | sed "s/^$IMAGE_PREFIX://")
  STARTED_AT=$(date -u '+%Y-%m-%dT%H:%M:%S')
  if [ -z "$CURRENT" ]; then
    TARGET="${TARGET:-unknown}"
    status_write "failed" "fetch" "读不到当前容器镜像（nas-curator 未在跑？），拒绝升级" \
      "读不到当前容器镜像" "false"
    rm -f "$UPGRADE_DIR/request.json"
    log "读不到当前容器镜像，退出"
    exit 1
  fi
  DEPLOYED="false"
  OLD="$CURRENT"
  local logfile="$UPGRADE_DIR/upgrade-$TARGET.log"
  : > "$logfile"
  log "升级 $CURRENT → $TARGET（dry-run=$DRY_RUN）"
  status_write "running" "fetch" "开始拉取最新代码" "" "false"

  # ---- 1. 取代码：优先用 VM 已同步的暂存目录；没有或版本不对时改试 git 拉取 ----
  # NAS 直连 GitHub 不可靠（见 0.1.4 报告：clone 常因 HTTP/2 中断或连接超时
  # 失败；muse 仓 .git 达 87MB），因此主路径是 VM 侧 cron 每小时同步好的代码
  # （/vol2/@appshare/nas-curator/upgrade-work/stage，见 scripts/sync-code-to-nas.sh）。
  SRC_DIR=""
  local staged_version
  staged_version=$(python3 -c "import json; print(json.load(open('$WORKDIR/stage/VERSION'))['version'])" 2>/dev/null || echo "")
  if [ "$staged_version" = "$TARGET" ]; then
    SRC_DIR="$WORKDIR/stage"
    log "使用 VM 已同步的代码（版本 $TARGET）"
  else
    log "暂存目录版本为 ${staged_version:-无}，改试 git 拉取"
    rm -rf "$WORKDIR/repo"
    local attempt
    for attempt in 1 2; do
      if git -c http.version=HTTP/1.1 clone --depth 1 --filter=blob:none --sparse \
          "$REPO" "$WORKDIR/repo" >> "$logfile" 2>&1 \
          && git -C "$WORKDIR/repo" sparse-checkout set nas-curator >> "$logfile" 2>&1; then
        break
      fi
      log "git clone 第 $attempt 次失败，5 秒后重试"
      rm -rf "$WORKDIR/repo"
      sleep 5
    done
    local fetched
    fetched=$(python3 -c "import json; print(json.load(open('$WORKDIR/repo/nas-curator/VERSION'))['version'])" 2>/dev/null || echo "")
    if [ "$fetched" = "$TARGET" ]; then
      SRC_DIR="$WORKDIR/repo/nas-curator"
      log "git 拉取成功，版本 $TARGET"
    else
      fail "fetch" "NAS 上没有目标版本 $TARGET 的代码（暂存目录版本 ${staged_version:-无}，git 拉取到的版本 ${fetched:-无}；VM 同步每小时一次，请稍后再试）"
    fi
  fi

  # ---- 2. 构建新镜像（NAS 出站下载常抖动：单次构建限时 15 分钟，失败重试一次）----
  status_write "running" "build" "构建新镜像 nas-curator:$TARGET" "" "false"
  local build_ok="false"
  local attempt
  for attempt in 1 2; do
    if timeout -k 30 900 $DOCKER build -t "$IMAGE_PREFIX:$TARGET" \
        --build-arg PIP_EXTRA_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
        "$SRC_DIR" >> "$logfile" 2>&1; then
      build_ok="true"
      break
    fi
    log "镜像构建第 $attempt 次失败（见 $logfile），准备重试"
  done
  if [ "$build_ok" != "true" ]; then
    fail "build" "镜像构建失败（两次尝试均未完成，见 $logfile）"
  fi
  log "镜像 nas-curator:$TARGET 构建成功"
  if [ "$DRY_RUN" = "true" ]; then
    status_write "dry-run-ok" "build" "dry-run：拉代码与构建镜像均成功（未动容器与数据库）" "" "false"
    rm -f "$UPGRADE_DIR/request.json"
    log "dry-run 完成"
    exit 0
  fi

  # 回滚前置检查：上一版镜像必须还在，否则不升级（无回滚路）
  $DOCKER image inspect "$IMAGE_PREFIX:$OLD" > /dev/null 2>&1 \
    || fail "build" "上一版镜像 nas-curator:$OLD 不存在，无法回滚，拒绝升级"

  # ---- 3. 备份数据库（容器内 VACUUM INTO，见 0.1.2/0.1.3 部署口径）----
  status_write "running" "backup" "备份数据库" "" "false"
  local bak="nas-curator.db.bak-$(date '+%Y%m%d-%H%M%S')-pre${TARGET}"
  if ! $DOCKER exec nas-curator python3 -c \
      "import sqlite3; c=sqlite3.connect('/data/nas-curator.db'); c.execute(\"VACUUM INTO '/data/$bak'\"); c.close()" \
      >> "$logfile" 2>&1; then
    fail "backup" "数据库备份失败（见 $logfile）"
  fi
  local bak_bytes
  bak_bytes=$(stat -c %s "$DATA_DIR/$bak" 2>/dev/null || echo 0)
  log "数据库已备份到 $bak（$bak_bytes 字节）"

  # ---- 4. 换 compose 并重建 ----
  status_write "running" "deploy" "替换 compose 并重建容器" "" "false"
  local new_compose="$SRC_DIR/fpk/app/docker/docker-compose.yaml"
  grep -q "image: $IMAGE_PREFIX:$TARGET" "$new_compose" \
    || fail "deploy" "新 compose 里镜像不是 $IMAGE_PREFIX:$TARGET"
  [ -f "$APPDIR/docker-compose.yaml.$OLD-bak" ] \
    || cp "$APPDIR/docker-compose.yaml" "$APPDIR/docker-compose.yaml.$OLD-bak"
  cp "$new_compose" "$APPDIR/docker-compose.yaml" \
    || fail "deploy" "复制新 compose 失败"
  DEPLOYED="true"
  if ! (cd "$APPDIR" && TRIM_SERVICE_PORT=$PORT TRIM_APPDEST_VOL=vol2 \
      TRIM_APPNAME=nas-curator $DOCKER compose -p nas-curator up -d --force-recreate \
      >> "$logfile" 2>&1); then
    fail "deploy" "容器重建失败（见 $logfile），开始回滚"
  fi

  # ---- 5. 验证 ----
  status_write "running" "verify" "验证新版本（healthy + 六页 + 链顺序与 OCR）" "" "false"
  if ! verify_running "$IMAGE_PREFIX:$TARGET" "300"; then
    fail "verify" "新版本验证失败，开始回滚"
  fi

  status_write "done" "done" "升级完成：$CURRENT → $TARGET" "" "false"
  rm -f "$UPGRADE_DIR/request.json"
  log "升级完成：$CURRENT → $TARGET"
  exit 0
}

main "$@"
