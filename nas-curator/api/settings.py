"""api.settings —— 设置查看 / 更新 / 凭据写入。

- GET：库、收件箱、阈值、模型端点、auto_apply、定时、凭据掩码、模型列表（掩码）。
  凭据永远只返回掩码（前 2 + **** + 后 2），绝不返回明文。
- PUT：只更新 thresholds / auto_apply / schedule 回写 libraries.json；
  库路径不允许经 API 改（改路径须手动改文件），返回里注明。
- PUT /models：替换整个模型列表（顺序即优先级 fallback 链）；每条可带 key，
  有 key 时写进项目 config/.env.local（600 权限），模型列表文件本身绝不含 Key。
- POST /key：兼容旧单框入口（页面已并入模型列表，API 保留）：key 写进
  config/.env.local（600 权限），同时写 os.environ。
"""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from common import llm_models as LM
from .deps import (ENV_LOCAL, LIBRARIES_JSON, get_settings, key_env_var,
                   load_libraries, mask_key, public_models, public_settings,
                   read_env_local)

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsPutBody(BaseModel):
    thresholds: dict | None = None
    auto_apply: bool | None = None
    schedule: dict | None = None


class KeyBody(BaseModel):
    provider: str
    key: str


class ModelsPutBody(BaseModel):
    models: list


def _write_env_key(env_var: str, key: str) -> None:
    """key 写进 .env.local（替换或追加该行，其余行原样保留），600 权限，
    同时注入当前进程环境。"""
    lines: list[str] = []
    try:
        with open(ENV_LOCAL, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    prefix = env_var + "="
    replaced = False
    out_lines = []
    for line in lines:
        if line.strip().startswith(prefix):
            out_lines.append(f"{env_var}={key}")
            replaced = True
        else:
            out_lines.append(line)
    if not replaced:
        out_lines.append(f"{env_var}={key}")
    try:
        os.makedirs(os.path.dirname(ENV_LOCAL), exist_ok=True)
        with open(ENV_LOCAL, "w", encoding="utf-8") as f:
            f.write("\n".join(out_lines) + "\n")
        os.chmod(ENV_LOCAL, 0o600)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"写 .env.local 失败：{e}")
    os.environ[env_var] = key


@router.get("")
def get():
    out = public_settings()
    models, source = public_models()
    out["models"] = models
    out["models_source"] = source
    return out


@router.put("")
def put(body: SettingsPutBody):
    cfg = load_libraries()
    changed = []
    if body.thresholds is not None:
        cfg.setdefault("thresholds", {}).update(body.thresholds)
        changed.append("thresholds")
    if body.auto_apply is not None:
        cfg["auto_apply"] = bool(body.auto_apply)
        changed.append("auto_apply")
    if body.schedule is not None:
        inbox = cfg.setdefault("inbox", {})
        if "inbox_scan_minutes" in body.schedule:
            inbox["scan_interval_minutes"] = int(body.schedule["inbox_scan_minutes"])
        if "settle_minutes" in body.schedule:
            inbox["settle_minutes"] = int(body.schedule["settle_minutes"])
        changed.append("schedule")
    if not changed:
        raise HTTPException(status_code=400,
                            detail="未提供可更新字段（thresholds/auto_apply/schedule）")
    try:
        with open(LIBRARIES_JSON, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"写回 libraries.json 失败：{e}")
    out = public_settings()
    out["updated"] = changed
    out["note"] = "库路径不允许经 API 修改，改路径须手动编辑 config/libraries.json。"
    return out


@router.put("/models")
def put_models(body: ModelsPutBody):
    """替换整个模型列表（顺序即优先级 fallback 链）。

    每条可带 key（明文只在本次请求里出现）：有 key 时写进 .env.local
    对应 key_env 变量并注入当前进程环境；无 key（空/缺失）则保留已存 Key。
    模型列表文件本身绝不含 Key；返回的列表只含掩码。
    """
    raw = body.models
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="models 须为列表")
    # 先取每条的 key（写库前剥离，绝不进模型列表文件）。
    keys = {}
    cleaned = []
    for i, m in enumerate(raw):
        if not isinstance(m, dict):
            raise HTTPException(status_code=400, detail=f"第 {i + 1} 个模型须为对象")
        m = dict(m)
        key = (m.pop("key", None) or "")
        key = key.strip() if isinstance(key, str) else ""
        cleaned.append(m)
        if key:
            keys[i] = key
    try:
        norm = LM.validate_models(cleaned)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    for i, key in keys.items():
        env_var = norm[i].get("key_env") or ""
        if not env_var:
            raise HTTPException(
                status_code=400,
                detail=f"模型 {norm[i].get('name')} 不需要 Key（本地 Ollama）")
        _write_env_key(env_var, key)
    try:
        LM.save_models(norm)
    except (OSError, ValueError) as e:
        raise HTTPException(status_code=500, detail=f"写模型列表失败：{e}")
    models, source = public_models()
    note = ("模型列表已保存（顺序即 fallback 优先级）；Key 已写 .env.local（600），"
            "页面只显掩码。收件箱监听下轮扫描即用新列表，无需重启容器。")
    if not any(m.get("type") == "ollama" and m.get("enabled") for m in norm):
        note += "注意：列表里没有启用的本地 Ollama，全链失败时将直接降级为规则判定。"
    if not norm:
        note += "注意：列表为空，LLM 不可用，分类只走规则。"
    return {"ok": True, "models": models, "models_source": source, "note": note}


@router.post("/key")
def set_key(body: KeyBody):
    """兼容旧单框入口（页面已并入模型列表，API 保留）：key 写 .env.local。

    provider 为 ark-plan/ark-payg（兼容 ark_plan/ark_payg 写法）时，
    按模型列表里对应模型的 key_env 写；无模型列表时按默认变量名写。
    """
    provider = (body.provider or "").strip()
    key = (body.key or "").strip()
    if not provider:
        raise HTTPException(status_code=400, detail="provider 不能为空")
    if not key:
        raise HTTPException(status_code=400, detail="key 不能为空")
    env_var = key_env_var(provider)
    _write_env_key(env_var, key)
    return {"ok": True, "provider": provider, "env_var": env_var,
            "masked": mask_key(key),
            "note": "key 已写入 config/.env.local（600）并注入当前进程环境；GET 只返回掩码。"}
