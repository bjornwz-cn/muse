#!/bin/bash
# Midconf VLM launcher (container-side): rq ids are read from the file here,
# avoiding nested $() expansion through ssh/host shells.
set -u
IDS=$(cat /data/midconf_all_photo_rqids.txt)
exec python3 /data/vlm_reclassify_midconf.py --rq-ids "$IDS" --kinds low_conf,dup,blur,ambiguous --workers 2 \
  --summary /data/year_vlm_summary_midconf.json \
  --heartbeat-id midconf-reclassify --heartbeat-label 中置信重分类 \
  > /data/year_vlm_midconf.log 2>&1
