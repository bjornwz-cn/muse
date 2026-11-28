#!/bin/bash
# r4796: livp Ark agent-plan 看静帧（容器内执行；workers 4，任务书上限）
python3 /data/vlm_video_remaining_ark.py \
  --rq-ids "$(cat /data/r4796_livp_ark_rqids.txt)" \
  --workers 4 \
  --summary /data/r4796_vlm_livp_ark.json \
  --heartbeat-id r4796-livp-vlm-ark \
  > /data/r4796_vlm_livp_ark.log 2>&1
