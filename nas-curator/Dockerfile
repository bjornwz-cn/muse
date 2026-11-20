# NAS Curator（文件管家）0.1.4 —— 飞牛 App 容器镜像
# 多架构：amd64 + arm64（buildx 构建；本次 VM 沙箱无法启动容器，
# 实际产物由手工组装脚本按同一份依赖清单生成，见 scripts/assemble-image.sh）。
# 底镜像 python:3.12-slim（与 P1/P2 实测的 Python 3.12 一致）。
# 凭据绝不进镜像：火山 key 只走运行时环境变量或安装后向导页写入的 .env.local。
# PIP_EXTRA_INDEX_URL：PyPI 附加源（默认 pypi.org；NAS 构建时传国内镜像，
# 如 --build-arg PIP_EXTRA_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/）。
ARG PIP_EXTRA_INDEX_URL=https://pypi.org/simple/
FROM python:3.12-slim AS base

LABEL org.opencontainers.image.title="NAS Curator" \
      org.opencontainers.image.description="文件管家：统一整理 NAS 文档与媒体库（飞牛 App）" \
      org.opencontainers.image.version="0.1.4"

# 文档抽取依赖（二进制工具，P1 实测口径）：
#   poppler-utils -> pdftotext（PDF 文本抽取；缺失时走远程 OCR 服务）
#   catdoc       -> .doc/.xls/.ppt 抽取（无纯 Python 回退）
#   ca-certificates -> 模型/服务 HTTPS
#   curl         -> 健康检查与调试（小）
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      poppler-utils catdoc ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# Python 依赖：torch 用 CPU index（不带 CUDA/nvidia 大包），其余走 PyPI。
# 版本锁见 requirements-image.lock（由安装脚本生成）。
# 注意：torch 主源必须是 download.pytorch.org/whl/cpu——2026-11-20 试过改用
# 阿里云 https://mirrors.aliyun.com/pytorch-wheels/cpu，结果它不是合法的
# PEP 503 Simple 索引（返回镜像站 About 页），pip 在该源找不到任何包，
# 静默回落到 PyPI 的 CUDA 版 torch（539MB + 数 GB nvidia 轮子）。已改回。
# torch 在 requirements-image.txt 里钉死 2.15.0+cpu，杜绝静默装 CUDA 版。
ARG PIP_EXTRA_INDEX_URL=https://pypi.org/simple/
COPY requirements-image.txt /tmp/requirements-image.txt
RUN pip install --no-cache-dir \
      --index-url https://download.pytorch.org/whl/cpu \
      --extra-index-url ${PIP_EXTRA_INDEX_URL} \
      -r /tmp/requirements-image.txt \
 && rm -f /tmp/requirements-image.txt

WORKDIR /app
COPY curator.py entrypoint.sh VERSION ./
COPY common/ ./common/
COPY curator_core/ ./curator_core/
COPY media_core/ ./media_core/
COPY api/ ./api/
COPY web/ ./web/
COPY config/ ./config/
# 运行时配置与 DB 不放镜像内：首次启动由 api.deps.ensure_config() 从镜像自带
# config/ 复制默认 libraries.json 到 /data/config；DB 在 /data/nas-curator.db。
RUN mkdir -p /data/config /data/logs \
 && chmod +x /app/entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    HF_HUB_CACHE=/vol2/@appdata/marvis-app/data/home/agent-memory/media-curator/models \
    CURATOR_DB=/data/nas-curator.db \
    CURATOR_CONFIG_DIR=/data/config \
    LLM_HOST=http://172.17.0.1:11434 \
    SEM_HOST=http://172.17.0.1:11500 \
    OCR_URL=http://172.17.0.1:8866

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/dashboard', timeout=5)"
ENTRYPOINT ["/app/entrypoint.sh"]
