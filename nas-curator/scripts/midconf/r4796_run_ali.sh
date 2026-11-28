#!/bin/bash
# r4796: 阿里 qwen4-max 看帧（容器内执行；Key 从 stdin 第一行读，绝不落盘）
python3 /data/vlm_video_remaining_ali.py \
  --rq-ids "$(cat /data/r4796_ali_rqids.txt)" \
  --workers 8 \
  --summary /data/r4796_vlm_ali.json \
  --heartbeat-id r4796-videos-vlm-ali
