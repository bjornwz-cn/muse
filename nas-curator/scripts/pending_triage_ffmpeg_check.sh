#!/bin/bash
# 宿主机侧：对复核 dead 的 386 个视频再跑 ffmpeg 单帧解码（第二意见）。
# 输入 NAS /data/pending_triage_verify.tsv（宿主机路径 /vol2/@appshare/nas-curator/data/）。
# 输出 /vol2/@appshare/nas-curator/data/pending_triage_ffmpeg_check.tsv
# 每行：rq_id \t src \t ffmpeg_result(ok|fail) \t first_error_line
IN=/vol2/@appshare/nas-curator/data/pending_triage_verify.tsv
OUT=/vol2/@appshare/nas-curator/data/pending_triage_ffmpeg_check.tsv
echo -e "rq_id\tsrc\tffmpeg\tfirst_error" > "$OUT"
n=0; ok=0; fail=0
while IFS=$'\t' read -r scope rq_id src size_list mtime_list size_disk mtime_disk sha verdict detail; do
  [ "$scope" = "scope" ] && continue
  [ "$verdict" != "dead" ] && continue
  n=$((n+1))
  err=$(timeout 120 ffmpeg -v error -i "$src" -frames:v 1 -f null - 2>&1 | head -1)
  if [ -z "$err" ]; then res=ok; ok=$((ok+1)); else res=fail; fail=$((fail+1)); fi
  printf '%s\t%s\t%s\t%s\n' "$rq_id" "$src" "$res" "$err" >> "$OUT"
  if [ $((n % 50)) -eq 0 ]; then echo "ffmpeg-check $n ok=$ok fail=$fail"; fi
done < "$IN"
echo "FFMPEG CHECK DONE n=$n ok=$ok fail=$fail"
