"""api.wizard —— 首次安装向导（Web 内三步设置）。

首次启动 Web 时若未完成向导（数据目录下无 .setup_done 标记），页面路由
自动跳到 /setup；向导完成后写配置并进总览。

三步：
  ① 确认库路径（工作 / 家庭 / 媒体 + 统一收件箱 + 笔记本备份输入源）
  ② 模型端点（ollama-cpu :11434 / ollama-gpu :11500 / OCR :8866）
     + 火山 key（可选；只存 config/.env.local 600，页面只显掩码）
  ③ 定时任务与自动执行开关（默认：收件箱每 30 分钟扫、自动执行开、阈值 0.85）

库路径允许在向导里改（与设置页"库路径不许经 API 改"不同：向导是首次安装，
改的是尚未生效的初始配置；一旦完成向导，后续改路径仍须手动改文件）。
"""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .deps import (ENV_LOCAL, LIBRARIES_JSON, get_settings, is_setup_done,
                   key_env_var, load_libraries, mark_setup_done, mask_key,
                   public_settings)

router = APIRouter(tags=["wizard"])

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(PROJECT_ROOT, "web", "templates")


class SetupBody(BaseModel):
    libraries: list | None = None          # [{id, root}]，只改 root
    inbox: dict | None = None              # {path, scan_interval_minutes, settle_minutes}
    input_sources: list | None = None      # [{id, root}]，只改 root
    model_endpoints: dict | None = None    # {llm_host, sem_host, ocr_url}
    thresholds: dict | None = None         # {auto, review_suggest, ...}
    auto_apply: bool | None = None
    keys: dict | None = None               # {ark_plan: "...", ark_payg: "..."}，空值跳过


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    return templates.TemplateResponse(request, "setup.html", {})


@router.get("/api/setup")
def setup_status():
    return {"setup_done": is_setup_done(), "settings": public_settings()}


def _write_libraries(cfg: dict) -> None:
    os.makedirs(os.path.dirname(LIBRARIES_JSON), exist_ok=True)
    with open(LIBRARIES_JSON, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _write_keys(keys: dict) -> dict:
    """key 写 .env.local（600）+ 注入当前进程环境；返回 provider → 掩码。"""
    out = {}
    for provider, key in (keys or {}).items():
        key = (key or "").strip()
        if not key:
            continue
        env_var = key_env_var(provider)
        lines: list[str] = []
        try:
            with open(ENV_LOCAL, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
        prefix = env_var + "="
        replaced = False
        new_lines = []
        for line in lines:
            if line.strip().startswith(prefix):
                new_lines.append(f"{env_var}={key}")
                replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            new_lines.append(f"{env_var}={key}")
        os.makedirs(os.path.dirname(ENV_LOCAL), exist_ok=True)
        with open(ENV_LOCAL, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines) + "\n")
        os.chmod(ENV_LOCAL, 0o600)
        os.environ[env_var] = key
        out[provider] = mask_key(key)
    return out


@router.post("/api/setup")
def setup_submit(body: SetupBody):
    cfg = load_libraries()
    changed: list[str] = []
    if body.libraries is not None:
        by_id = {lib.get("id"): lib for lib in cfg.get("libraries", [])}
        for item in body.libraries:
            lib = by_id.get(item.get("id"))
            if lib and item.get("root"):
                lib["root"] = item["root"]
        changed.append("libraries")
    if body.input_sources is not None:
        by_id = {s.get("id"): s for s in cfg.get("input_sources", [])}
        for item in body.input_sources:
            src = by_id.get(item.get("id"))
            if src and item.get("root"):
                src["root"] = item["root"]
        changed.append("input_sources")
    if body.inbox is not None:
        inbox = cfg.setdefault("inbox", {})
        if body.inbox.get("path"):
            inbox["path"] = body.inbox["path"]
        if "scan_interval_minutes" in body.inbox:
            inbox["scan_interval_minutes"] = int(body.inbox["scan_interval_minutes"])
        if "settle_minutes" in body.inbox:
            inbox["settle_minutes"] = int(body.inbox["settle_minutes"])
        changed.append("inbox")
    if body.model_endpoints is not None:
        me = cfg.setdefault("model_endpoints", {})
        for k in ("llm_host", "sem_host", "ocr_url"):
            if body.model_endpoints.get(k):
                me[k] = body.model_endpoints[k]
        changed.append("model_endpoints")
    if body.thresholds is not None:
        cfg.setdefault("thresholds", {}).update(body.thresholds)
        changed.append("thresholds")
    if body.auto_apply is not None:
        cfg["auto_apply"] = bool(body.auto_apply)
        changed.append("auto_apply")
    try:
        _write_libraries(cfg)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"写回 libraries.json 失败：{e}")
    masked = _write_keys(body.keys or {})
    mark_setup_done()
    out = get_settings()
    out.pop("_raw", None)
    out["setup_done"] = True
    out["updated"] = changed
    out["keys"] = masked
    return out
