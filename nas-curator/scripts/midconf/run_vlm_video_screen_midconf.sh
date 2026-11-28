#!/bin/bash
# Midconf video stage-1 screen launcher (container-side).
set -u
IDS=$(cat /data/midconf_video_frame_ok.txt)
exec python3 /data/vlm_video_screen_midconf.py --rq-ids "$IDS" --workers 4 \
  --summary /data/year_vlm_summary_midconf_video_screen.json \
  --heartbeat-id midconf-video-screen --heartbeat-label 中置信视频model-3过筛 \
  > /data/year_vlm_midconf_video_screen.log 2>&1
