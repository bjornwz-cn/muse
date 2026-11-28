"""api.deps —— 共享依赖：DB 连接、配置读取。"""
from __future__ import annotations

import json
import os
import sqlite3

from common import db as DB
from common.llm_models import mask_key  # 掩码规范出处（前 2 + **** + 后 2）

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED_CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
# 容器内配置目录走数据卷（CURATOR_CONFIG_DIR=/data/config）；未指定则用项目自带 config/。
CONFIG_DIR = os.environ.get("CURATOR_CONFIG_DIR") or SHIPPED_CONFIG_DIR
LIBRARIES_JSON = os.path.join(CONFIG_DIR, "libraries.json")
ENV_LOCAL = os.path.join(CONFIG_DIR, ".env.local")


def get_upgrade_dir() -> str:
    """升级工作目录：与 DB 文件同目录下的 upgrade/（检查缓存、请求标志、状态文件）。

    容器内为 /data/upgrade；本地开发为项目 data/upgrade。
    """
    return os.path.join(os.path.dirname(get_db_path()), "upgrade")


def get_setup_marker() -> str:
    """首次安装向导完成标记：与 DB 文件同目录的 .setup_done。"""
    return os.path.join(os.path.dirname(get_db_path()), ".setup_done")


def is_setup_done() -> bool:
    return os.path.exists(get_setup_marker())


def mark_setup_done() -> None:
    marker = get_setup_marker()
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    with open(marker, "w", encoding="utf-8") as f:
        f.write("setup done\n")


def ensure_config() -> None:
    """容器首次启动：数据卷配置目录缺失时，从镜像自带 config/ 复制默认配置。

    只复制 libraries.json（不含任何凭据）；.env.local 绝不复制（向导页输入后才生成）。
    本地开发（CONFIG_DIR == 自带目录）时什么都不做。
    """
    if os.path.abspath(CONFIG_DIR) == os.path.abspath(SHIPPED_CONFIG_DIR):
        return
    if os.path.exists(LIBRARIES_JSON):
        return
    os.makedirs(CONFIG_DIR, exist_ok=True)
    shipped = os.path.join(SHIPPED_CONFIG_DIR, "libraries.json")
    if os.path.exists(shipped):
        import shutil
        shutil.copyfile(shipped, LIBRARIES_JSON)

DEFAULT_DB = os.path.join(PROJECT_ROOT, "data", "nas-curator.db")

# 模型端点默认值（与 curator.py build_parser 的默认值一致；只读配置，不主动连接）
DEFAULT_LLM_HOST = "http://172.17.0.1:11434"
DEFAULT_SEM_HOST = "http://172.17.0.1:11500"
# 0.1.3：默认 OCR 地址改为容器内可达的宿主网关地址。127.0.0.1 在容器内
# 指向容器自身，ocr-service 在宿主 docker 网桥上，那里永远不通；
# 显式配置（libraries.json / OCR_URL 环境变量）仍优先于此默认值。
DEFAULT_OCR_URL = "http://172.17.0.1:8866"

# 凭据 provider → 环境变量名（只做映射，值绝不进代码/日志）
KEY_ENV_VARS = {
    "ark_plan": "ARK_PLAN_KEY",
    "ark_payg": "ARK_PAYG_KEY",
}


def get_db_path() -> str:
    """DB 路径：环境变量 CURATOR_DB，否则项目 data/nas-curator.db（与 CLI 默认一致）。"""
    return os.environ.get("CURATOR_DB") or DEFAULT_DB


def get_db():
    """每请求一个连接，用完关闭。check_same_thread=False（FastAPI 线程池）。"""
    conn = sqlite3.connect(get_db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")  # 与 DB.connect 一致：Web 与收件箱监听并发写
    try:
        yield conn
    finally:
        conn.close()


def load_libraries() -> dict:
    """读 config/libraries.json；读不到返回空骨架（不抛错，接口降级返回空列表）。"""
    try:
        with open(LIBRARIES_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"libraries": [], "inbox": {}, "thresholds": {}, "excluded": []}


def load_media_config() -> dict:
    """读 media_core/config.json（媒体阈值 / auto_apply 兜底来源）。"""
    try:
        with open(os.path.join(PROJECT_ROOT, "media_core", "config.json"),
                  encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def get_settings() -> dict:
    """汇总设置：libraries.json + 媒体配置兜底 + 模型端点默认值。

    auto_apply 口径：libraries.json 顶层 auto_apply（PUT /api/settings 可写）；
    未设置时取 media_core/config.json 的 auto_apply；再没有则 False。
    """
    cfg = load_libraries()
    media_cfg = load_media_config()
    thresholds = dict(cfg.get("thresholds") or {})
    thresholds.setdefault("auto", 0.85)
    thresholds.setdefault("auto_doc_llm", 0.75)
    thresholds.setdefault("auto_media", 0.65)
    thresholds.setdefault("auto_media_margin", 0.15)
    thresholds.setdefault("review_suggest", 0.60)
    me = dict(cfg.get("model_endpoints") or {})
    # 0.1.5：向量模型独立配置（embedding_model 只读 host=SEM_HOST）；
    # 图片扩展名（image_extensions）两引擎共用，缺省 16 个。
    from common.embedding import load_embedding_config
    from common.image_ext import get_image_exts
    emb = load_embedding_config()
    model_endpoints = {
        "llm_host": me.get("llm_host") or os.environ.get("LLM_HOST") or DEFAULT_LLM_HOST,
        "sem_host": emb["host"],
        "ocr_url": me.get("ocr_url") or os.environ.get("OCR_URL") or DEFAULT_OCR_URL,
        "embedding_model": emb["model"],
    }
    inbox = dict(cfg.get("inbox") or {})
    schedule = {
        "inbox_scan_minutes": inbox.get("scan_interval_minutes", 30),
        "settle_minutes": inbox.get("settle_minutes", 3),
    }
    auto_apply = cfg.get("auto_apply")
    if auto_apply is None:
        auto_apply = bool(media_cfg.get("auto_apply", False))
    return {
        "libraries": cfg.get("libraries", []),
        "input_sources": cfg.get("input_sources", []),
        "inbox": inbox,
        "excluded": cfg.get("excluded", []),
        "thresholds": thresholds,
        "model_endpoints": model_endpoints,
        "embedding": {"host": emb["host"], "model": emb["model"]},
        "image_extensions": sorted(get_image_exts()),
        "auto_apply": bool(auto_apply),
        "schedule": schedule,
        "_raw": cfg,  # 内部用：写回时以此为底
    }


def public_settings() -> dict:
    """GET /api/settings 的返回体：get_settings() 去内部键 + 掩码后的凭据。

    0.1.3：附带 ocr_status（OCR 端点实时可达状态，供设置页/向导页显示；
    探活超时 1.5 秒，不可达时不阻塞返回）。
    0.1.5：附带 embedding_status（向量模型端点探活 + 模型是否在列表里）
    与 vision_status（SigLIP / ChineseCLIP 本地缓存状态；VLM 暂未接入）。
    """
    s = get_settings()
    s.pop("_raw", None)
    s["keys"] = masked_keys()
    s["ocr_status"] = probe_ocr((s.get("model_endpoints") or {}).get("ocr_url") or "")
    emb = s.get("embedding") or {}
    s["embedding_status"] = probe_embedding(
        emb.get("host") or "", emb.get("model") or "")
    s["vision_status"] = vision_status()
    return s


def probe_embedding(host: str, model: str, timeout: float = 1.5) -> dict:
    """向量模型端点探活：GET {host}/api/tags，看服务可达与模型是否在列表里。"""
    import time
    import urllib.error
    import urllib.request
    if not host:
        return {"host": host, "model": model, "reachable": False,
                "status": None, "has_model": False, "error": "未配置 endpoint"}
    t0 = time.time()
    try:
        req = urllib.request.Request(host.rstrip("/") + "/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
            names = [m.get("name") for m in (body.get("models") or [])]
            return {"host": host, "model": model, "reachable": True,
                    "status": r.status, "has_model": model in names,
                    "ms": int((time.time() - t0) * 1000)}
    except urllib.error.HTTPError as e:
        return {"host": host, "model": model, "reachable": True,
                "status": e.code, "has_model": False,
                "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"host": host, "model": model, "reachable": False,
                "status": None, "has_model": False,
                "ms": int((time.time() - t0) * 1000), "error": type(e).__name__}


def vision_status() -> dict:
    """视觉模型状态：当前固定 SigLIP + ChineseCLIP（本地缓存打分）。

    检查 media_core/config.json 的 model_dir 下两个快照目录是否存在；
    视觉大模型（VLM）暂未接入，只留位置与说明。
    """
    cfg = load_media_config()
    model_dir = cfg.get("model_dir") or ""
    siglip = os.path.join(model_dir, "models--timm--ViT-B-16-SigLIP")
    cnclip = os.path.join(model_dir, "models--OFA-Sys--chinese-clip-vit-base-patch16")
    siglip_ok = bool(model_dir) and os.path.isdir(siglip)
    cnclip_ok = bool(model_dir) and os.path.isdir(cnclip)
    return {
        "models": ["SigLIP ViT-B-16-SigLIP (webli)",
                   "ChineseCLIP vit-base-patch16"],
        "model_dir": model_dir,
        "siglip_present": siglip_ok,
        "cnclip_present": cnclip_ok,
        "ready": siglip_ok and cnclip_ok,
        "vlm": None,
        "note": "视觉大模型（VLM）暂未接入；当前只用本地 SigLIP + ChineseCLIP 打分。",
    }


def probe_ocr(url: str, timeout: float = 1.5) -> dict:
    """探活 OCR 端点：GET /ocr，任意 HTTP 状态即为可达
    （该端点只接受 POST，GET 返回 405 也算可达）；连接失败则不可达。"""
    import time
    import urllib.error
    import urllib.request
    url = (url or "").rstrip("/")
    t0 = time.time()
    if not url:
        return {"url": "", "reachable": False, "status": None, "ms": 0}
    try:
        req = urllib.request.Request(url + "/ocr", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"url": url, "reachable": True, "status": r.status,
                    "ms": int((time.time() - t0) * 1000)}
    except urllib.error.HTTPError as e:
        return {"url": url, "reachable": True, "status": e.code,
                "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"url": url, "reachable": False, "status": None,
                "ms": int((time.time() - t0) * 1000), "error": type(e).__name__}


def read_env_local() -> dict:
    """解析 config/.env.local（KEY=VALUE 行，# 注释跳过）。不存在返回 {}。"""
    out = {}
    try:
        with open(ENV_LOCAL, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def masked_keys() -> dict:
    """已配置的 provider → 掩码值。只读 .env.local 与环境变量，绝不返回明文。"""
    stored = read_env_local()
    out = {}
    for provider, env_var in KEY_ENV_VARS.items():
        value = stored.get(env_var) or os.environ.get(env_var) or ""
        if value:
            out[provider] = mask_key(value)
    return out


def public_models() -> tuple:
    """模型列表公开视图（每条含 key_set/key_masked，绝不含明文）+ 来源。

    来源 'file' = 用户在设置页保存过的列表；'default' = 代码默认列表
    （火山 Ark Plan → 火山 Ark 按量 → 本地 Ollama）。
    """
    from common import llm_models as LM
    models, source = LM.load_models()
    stored = read_env_local()
    key_values = {}
    for m in models:
        env_var = m.get("key_env") or ""
        if env_var:
            key_values[env_var] = stored.get(env_var) or os.environ.get(env_var) or ""
    return LM.public_models(models, key_values), source


def key_env_var(provider: str) -> str:
    """provider → 环境变量名；未知 provider 按 <PROVIDER>_KEY 通用规则。"""
    return KEY_ENV_VARS.get(provider) or (provider.strip().upper().replace("-", "_") + "_KEY")
