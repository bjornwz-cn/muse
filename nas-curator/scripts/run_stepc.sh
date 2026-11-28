#!/bin/bash
# 步骤 C 复检总编排（容器内执行，detached 运行）：
#   0. 备份 DB（sqlite3 backup API，备份名按 CST 时间）
#   1. SigLIP（照片/视频，fresh 分类，结果写 year_siglip_results_stepc.jsonl）
#   2. 文档 LLM（文档/其他，追加同一 JSONL）
#   3. 登记（把 JSONL 结论写回已有 review 行，只更新、不建行；损坏只标记不删）
#   4. VLM（登记产出的低置信图片候选，decided_by='vlm-stepc'）
# 各阶段顺序执行，前一阶段失败则停止（set -e）。全程不移动、不删除任何文件。
set -e -u
cd /data
echo "=== stepc start $(date -u '+%Y-%m-%dT%H:%M:%SZ') UTC ==="

# 0. 备份 DB
python3 - <<'PYEOF'
import os, sqlite3
from datetime import datetime, timedelta, timezone
cst = timezone(timedelta(hours=8))
ts = datetime.now(cst).strftime("%Y%m%d-%H%M")
bak = "/data/nas-curator.db.bak-%s-pre-stepc" % ts
s = sqlite3.connect("/data/nas-curator.db")
d = sqlite3.connect(bak)
s.backup(d)
d.close(); s.close()
print("backup: %s (%d bytes)" % (bak, os.path.getsize(bak)), flush=True)
PYEOF

# 1. SigLIP
echo "=== phase siglip ==="
python3 /data/year_siglip_stepc.py --workers 4 --files-from /data/stepc_snapshot.tsv \
  > /data/year_siglip_stepc.log 2>&1
echo "siglip done"

# 2. 文档
echo "=== phase doc ==="
python3 /data/year_doc_stepc.py --workers 2 \
  > /data/year_doc_stepc.log 2>&1
echo "doc done"

# 3. 登记
echo "=== phase register ==="
python3 /data/year_register_stepc.py \
  > /data/year_register_stepc.log 2>&1
echo "register done"

# 4. VLM（仅当候选文件非空）
if [ -s /data/stepc_vlm_rqids.txt ]; then
  echo "=== phase vlm ==="
  IDS=$(cat /data/stepc_vlm_rqids.txt)
  python3 /data/vlm_reclassify_stepc.py --rq-ids "$IDS" --workers 2 \
    --kinds "low_conf,ambiguous,dup,blur" \
    --summary /data/year_vlm_summary_stepc.json \
    --heartbeat-id vlm-stepc-recheck --heartbeat-label "步骤 C VLM 复检" \
    > /data/year_vlm_stepc.log 2>&1
  echo "vlm done"
else
  echo "=== phase vlm skipped (no candidates) ==="
fi

echo "=== stepc ALL DONE $(date -u '+%Y-%m-%dT%H:%M:%SZ') UTC ==="
