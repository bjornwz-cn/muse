#!/bin/bash
# Midconf stage-2 confirm launcher (container-side).
# Confirm set (/data/midconf_confirm_rqids.txt) is generated from the stage-1
# summary AFTER stage 1 finishes: economy>=0.72 + stage-1 api_error rows.
set -u
IDS=$(cat /data/midconf_confirm_rqids.txt)
exec python3 /data/vlm_confirm_midconf.py --rq-ids "$IDS" --kinds low_conf,dup,blur,ambiguous --workers 4 \
  --summary /data/year_vlm_summary_midconf_confirm.json \
  --heartbeat-id midconf-reclassify --heartbeat-label 中置信重分类 \
  > /data/year_vlm_midconf_confirm.log 2>&1
