#!/bin/bash
# Batch4 VLM launcher (container-side): rq ids are read from the file here,
# avoiding nested $() expansion through ssh/host shells.
set -u
IDS=$(cat /data/year_vlm_rqids_batch4.txt)
exec python3 /data/vlm_reclassify_batch4.py --rq-ids "$IDS" --workers 2 \
  --summary /data/year_vlm_summary_batch4.json \
  --heartbeat-id year-reclassify-batch4 --heartbeat-label 第四批分类 \
  > /data/year_vlm_batch4.log 2>&1
