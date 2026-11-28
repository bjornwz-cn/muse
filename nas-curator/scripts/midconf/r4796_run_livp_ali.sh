#!/bin/bash
# r4796: livp 阿里 qwen4-max 看静帧（容器内执行；Key 从 stdin 第一行读，绝不落盘）
python3 /data/vlm_video_remaining_ali.py \
  --rq-ids "$(cat /data/r4796_livp_ali_rqids.txt)" \
  --workers 4 \
  --summary /data/r4796_vlm_livp_ali.json \
  --heartbeat-id r4796-livp-vlm-ali
