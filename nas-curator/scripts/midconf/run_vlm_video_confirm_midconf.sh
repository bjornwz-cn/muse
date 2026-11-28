#!/bin/bash
# Midconf video stage-2 confirm launcher (container-side).
set -u
IDS=$(cat /data/midconf_video_confirm_rqids.txt)
exec python3 /data/vlm_video_confirm_midconf.py --rq-ids "$IDS" --workers 4 \
  --summary /data/year_vlm_summary_midconf_video_confirm.json \
  --heartbeat-id midconf-video-frames --heartbeat-label 中置信视频看帧 \
  > /data/year_vlm_midconf_video_confirm.log 2>&1
