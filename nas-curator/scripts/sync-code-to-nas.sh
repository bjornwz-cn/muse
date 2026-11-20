#!/bin/bash
# scripts/sync-code-to-nas.sh —— VM 侧代码同步（0.1.4）。
#
# 背景：NAS 直连 GitHub 不可靠——git ls-remote 与 api.github.com 通，
# 但 git clone 常失败（HTTP/2 stream 中断、HTTP/1.1 连接超时 134s；
# muse 仓 .git 达 87MB，而 nas-curator/ 子树仅 1.4MB）。
# 因此升级用的代码改由本 VM 定时同步到 NAS：
#   git archive（只取已提交的 origin/main 的 nas-curator/ 子树，绝不含
#   未提交改动与 .env.local）→ 经 SSH 隧道 tar 到 NAS 暂存目录。
# 本脚本由 VM 侧 cron 每小时跑一次；HEAD 未变时直接退出。
# 升级脚本 upgrade.sh 优先用这里同步好的代码，git clone 只作后备。
set -uo pipefail

REPO_DIR="$HOME/workspace/repos/muse"
NAS_STAGE="/vol2/@appshare/nas-curator/upgrade-work/stage"
STATE_DIR="$HOME/workspace/nas-curator-sync"
STATE_FILE="$STATE_DIR/state"
NAS_SSH="$HOME/workspace/tools/nas-ssh.sh"
# 与 git push 同一路 SSH（见 0.1.2/0.1.3 推送口径）：默认 ssh 在非交互环境下
# 会因 host key 校验失败（"Host key verification failed"）。
export GIT_SSH_COMMAND="ssh -i /home/hatch/.ssh/id_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/home/hatch/.ssh/known_hosts"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] sync-code-to-nas: $*"; }

mkdir -p "$STATE_DIR"

# 1. 拉取远端（VM → GitHub 直连正常）
if ! git -C "$REPO_DIR" fetch origin --quiet 2>&1; then
  log "git fetch origin 失败，退出（下次再试）"
  exit 1
fi
HEAD=$(git -C "$REPO_DIR" rev-parse origin/main)
LAST=$(cat "$STATE_FILE" 2>/dev/null | head -1 || echo "")
if [ "$HEAD" = "$LAST" ]; then
  log "HEAD 未变（$HEAD），无需同步"
  exit 0
fi

# 2. 只归档 nas-curator/ 子树，经隧道传到 NAS 暂存目录
if ! git -C "$REPO_DIR" archive origin/main nas-curator \
    | "$NAS_SSH" "rm -rf $NAS_STAGE && mkdir -p $NAS_STAGE && tar -xf - -C $NAS_STAGE --strip-components=1"; then
  log "传输到 NAS 失败，退出（下次再试）"
  exit 1
fi

# 3. 核对 NAS 侧版本文件与 HEAD 一致
NAS_VERSION=$("$NAS_SSH" "cat $NAS_STAGE/VERSION 2>/dev/null" \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('version',''))" 2>/dev/null || echo "")
LOCAL_VERSION=$(git -C "$REPO_DIR" show origin/main:nas-curator/VERSION \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('version',''))")
if [ -z "$NAS_VERSION" ] || [ "$NAS_VERSION" != "$LOCAL_VERSION" ]; then
  log "核对失败：NAS 侧版本 $NAS_VERSION，远端 $LOCAL_VERSION"
  exit 1
fi

echo "$HEAD" > "$STATE_FILE"
log "已同步 $HEAD（nas-curator $NAS_VERSION）到 NAS $NAS_STAGE"
exit 0
