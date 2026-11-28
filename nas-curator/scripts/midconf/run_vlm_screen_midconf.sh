#!/bin/bash
# Midconf stage-1 screen launcher (container-side).
set -u
IDS=$(cat /data/midconf_all_photo_rqids.txt)
exec python3 /data/vlm_screen_midconf.py --rq-ids "$IDS" --kinds low_conf,dup,blur,ambiguous --workers 4 \
  --summary /data/year_vlm_summary_midconf_screen.json \
  --heartbeat-id midconf-screen --heartbeat-label 中置信model-3过筛 \
  > /data/year_vlm_midconf_screen.log 2>&1
