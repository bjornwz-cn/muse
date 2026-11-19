#!/bin/bash
# P4 修库 dry-run：文档三批（工作/家庭/Wangzhen-Mac），只做计划、不移动文件。
# 在 nas-curator 容器内跑（网络与模型端点与 App 一致）。每批一个 plan 行，
# CSV 落 /data/plan_repair_*.csv，日志落 /data/logs/repair-docs.log。
# 工作/家庭按顶级单元目录逐个做 root（库根只产出单元行；归档是 frozen，
# 须以归档本身为 root 才能扫到）；Wangzhen-Mac 走 mixed（文档归工作库、
# 媒体归媒体库，家庭分流启发式）。
# 用法（容器内）：bash /data/repair_docs.sh
set -u
DB=/data/nas-curator.db
run() {
  local name="$1"; shift
  echo "=== [$name] $(date '+%F %T') start: $* ==="
  python3 /app/curator.py --db "$DB" plan "$@" --deep --llm --sem --no-ocr \
    --out "/data/plan_repair_${name}.csv"
  echo "=== [$name] $(date '+%F %T') exit=$? ==="
}
# --- 工作文件：4 个顶级单元各做一次 ---
for d in 1-王震 2-上海润心 归档 收件箱; do
  safe=$(echo "$d" | tr -cd '0-9a-zA-Z')
  run "work_${safe}" --root "/vol1/1000/工作文件/${d}" --library work
done
# --- 家庭文件：按人目录各做一次（空目录会产出 0 项计划，正常）---
for d in 刘音 王子婴 王子轩 王现琦 王震 韩红; do
  safe=$(echo "$d" | tr -cd '0-9a-zA-Z')
  run "family_${safe}" --root "/vol1/1000/家庭文件/${d}" --library family
done
# --- Wangzhen-Mac：mixed 输入源（P1 规则层 auto 产出 1,134 项；本次 deep+llm+sem）---
run "macbackup" --root /vol1/1000/下载/Wangzhen-Mac --library mac-backup
echo "=== ALL DOC BATCHES DONE $(date '+%F %T') ==="
