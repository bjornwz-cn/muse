#!/bin/bash
# NAS Curator 容器入口：起 Web 服务 + 收件箱监听两个进程。
# 以 root 运行（fnOS 惯例，见 README/报告）：/vol1/1000 为 mode 000 + trimacl，
# 只有 root 与属主 bjorn（uid 1000）可读写；容器内以 root 跑可确保读写，
# 归位后文件仍 chown 1000:1000（curator.py 的 apply 逻辑），属主不变。
# 凭据：只走环境变量（compose 注入或向导页写入 .env.local 后由 api 进程加载），
# 本脚本不写、不回显任何 key。
set -e
APP=/app
SP=/usr/local/lib/python3.12/site-packages
# cv2/torch 的 .libs 不在默认 so 搜索路径（P1 遗留 2）：显式加 LD_LIBRARY_PATH
export LD_LIBRARY_PATH="$SP/opencv_python_headless.libs:$SP/opencv_contrib_python.libs:$SP/torch/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export CURATOR_DB="${CURATOR_DB:-/data/nas-curator.db}"
export CURATOR_CONFIG_DIR="${CURATOR_CONFIG_DIR:-/data/config}"
export HF_HUB_OFFLINE=1
# 让 open_clip.get_tokenizer 直接在本地模型缓存里找分词器（否则它只查
# huggingface.co 与默认 HF 缓存，离线时失败、走 HFTokenizer 兜底；
# 兜底本身可用，但直接路径更干净。缓存挂载为只读，离线只读不写。）
export HF_HUB_CACHE="${HF_HUB_CACHE:-/vol2/@appdata/marvis-app/data/home/agent-memory/media-curator/models}"
mkdir -p /data/config /data/logs

# 启动自检：把关键挂载的可写性写进日志（装完验证时看这一行就知道权限通没通）
for d in /vol1/1000 /data; do
  if [ -w "$d" ]; then echo "[entrypoint] writable: $d"; else echo "[entrypoint] NOT WRITABLE: $d" >&2; fi
done

# 收件箱监听（后台）：每 30 分钟扫一次（config 的 scan_interval_minutes），
# 全判定链 + 门控自动执行（低置信只进复核队列，不自动归位）。
# --out 指到持久卷 /data（默认会写到镜像内 /app/data，容器重建即丢）。
python "$APP/curator.py" --db "$CURATOR_DB" inbox --watch --apply --llm --sem \
  --llm-host "${LLM_HOST:-http://172.17.0.1:11434}" \
  --ocr-url "${OCR_URL:-http://172.17.0.1:8866}" \
  --out /data/plan_inbox.csv \
  >> /data/logs/inbox-watch.log 2>&1 &
WATCH_PID=$!

# Web 服务（后台，前台等任一进程退出即收尾）
python -m uvicorn api.main:app --host 0.0.0.0 --port 8000 --app-dir "$APP" \
  >> /data/logs/web.log 2>&1 &
UVICORN_PID=$!

_term() {
  kill "$WATCH_PID" "$UVICORN_PID" 2>/dev/null || true
}
trap _term TERM INT
wait -n "$WATCH_PID" "$UVICORN_PID" 2>/dev/null || true
_term
wait 2>/dev/null || true
