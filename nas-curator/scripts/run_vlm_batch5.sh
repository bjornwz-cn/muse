#!/bin/bash
# Batch5 VLM launcher (container-side): rq ids are read from the file here,
# avoiding nested $() expansion through ssh/host shells.
set -u
IDS=$(cat /data/year_vlm_rqids_batch5.txt)
exec python3 /data/vlm_reclassify_batch5.py --rq-ids "$IDS" --workers 2 \
  --summary /data/year_vlm_summary_batch5.json \
  --heartbeat-id year-reclassify-batch5 --heartbeat-label 第五批分类 \
  > /data/year_vlm_batch5.log 2>&1
