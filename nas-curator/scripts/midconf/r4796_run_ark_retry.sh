#!/bin/bash
# r4796: Ark agent-plan 重试 429 失败行（workers 3，任务书上限 4）
python3 /data/vlm_video_remaining_ark.py \
  --rq-ids "$(cat /data/r4796_ark_retry_rqids.txt)" \
  --workers 3 \
  --summary /data/r4796_vlm_ark_retry.json \
  --heartbeat-id r4796-videos-vlm-ark-retry \
  > /data/r4796_vlm_ark_retry.log 2>&1
