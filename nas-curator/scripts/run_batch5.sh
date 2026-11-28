#!/bin/bash
# 第五批分类总编排（容器内执行，detached 运行）：
#   0. 备份 DB（sqlite3 backup API，备份名按 CST 时间）
#   1. SigLIP（照片/视频，快照文件，结果写 year_siglip_results_batch5.jsonl）
#   2. 文档 LLM（文档/其他，追加同一 JSONL）
#   3. 登记（把 JSONL 结论写成 review 行，decided_by siglip-b5/doc-b5）
#   4. VLM（登记产出的低置信图片候选，decided_by='vlm-b5'）
#   5. 汇总（year_reclassify_summary_batch5.json）
# 各阶段顺序执行，前一阶段失败则停止（set -e）。全程不移动、不删除任何文件。
set -e -u
cd /data
echo "=== batch5 start $(date -u '+%Y-%m-%dT%H:%M:%SZ') UTC ==="

# 0. 备份 DB
python3 - <<'PYEOF'
import os, sqlite3
from datetime import datetime, timedelta, timezone
cst = timezone(timedelta(hours=8))
ts = datetime.now(cst).strftime("%Y%m%d-%H%M")
bak = "/data/nas-curator.db.bak-%s-pre-batch5" % ts
s = sqlite3.connect("/data/nas-curator.db")
d = sqlite3.connect(bak)
s.backup(d)
d.close(); s.close()
print("backup: %s (%d bytes)" % (bak, os.path.getsize(bak)), flush=True)
PYEOF

# 1. SigLIP
echo "=== phase siglip ==="
python3 /data/year_siglip_batch5.py --workers 4 --files-from /data/year_batch5_snapshot.tsv \
  > /data/year_siglip_batch5.log 2>&1
echo "siglip done"

# 2. 文档
echo "=== phase doc ==="
python3 /data/year_doc_batch5.py --workers 2 \
  > /data/year_doc_batch5.log 2>&1
echo "doc done"

# 3. 登记
echo "=== phase register ==="
python3 /data/year_register_batch5.py \
  > /data/year_register_batch5.log 2>&1
echo "register done"

# 4. VLM（仅当候选文件非空）
if [ -s /data/year_vlm_rqids_batch5.txt ]; then
  echo "=== phase vlm ==="
  bash /data/run_vlm_batch5.sh
  echo "vlm done"
else
  echo "=== phase vlm skipped (no candidates) ==="
fi

# 5. 汇总
echo "=== phase summary ==="
python3 /data/year_summary_batch5.py \
  > /data/year_summary_batch5.log 2>&1
echo "summary done"

echo "=== batch5 ALL DONE $(date -u '+%Y-%m-%dT%H:%M:%SZ') UTC ==="
