#!/bin/bash
# Midconf video-frame VLM launcher (container-side).
set -u
IDS=$(cat /data/midconf_video_frame_ok.txt)
exec python3 /data/vlm_video_frames_midconf.py --rq-ids "$IDS" --workers 2 \
  --summary /data/year_vlm_summary_midconf_video.json \
  --heartbeat-id midconf-video-frames --heartbeat-label 中置信视频看帧 \
  > /data/year_vlm_midconf_video.log 2>&1
