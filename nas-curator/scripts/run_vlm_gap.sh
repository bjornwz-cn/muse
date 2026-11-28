#!/bin/bash
# VLM 缺口补跑启动器（容器内执行）：rq id 从文件读，避免 ssh 嵌套 $() 展开问题。
# 只跑回退后在盘的缺口 id（/data/year_vlm_gap_rqids.txt），结论 decided_by='vlm-b4-gap'。
set -u
IDS=$(cat /data/year_vlm_gap_rqids.txt)
exec python3 /data/vlm_reclassify_batch4_gap.py --rq-ids "$IDS" --workers 2 \
  --summary /data/year_vlm_summary_batch4_gap.json \
  --heartbeat-id vlm-gap-rerun --heartbeat-label "VLM 缺口补跑" \
  > /data/year_vlm_batch4_gap.log 2>&1
