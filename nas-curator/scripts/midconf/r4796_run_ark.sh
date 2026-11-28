#!/bin/bash
# r4796: Ark agent-plan 看帧（容器内执行；rq 列表读容器内 /data 文件，避免宿主机 shell 展开）
python3 /data/vlm_video_remaining_ark.py \
  --rq-ids "$(cat /data/r4796_ark_rqids.txt)" \
  --workers 8 \
  --summary /data/r4796_vlm_ark.json \
  --heartbeat-id r4796-videos-vlm-ark \
  > /data/r4796_vlm_ark.log 2>&1
