"""api.settings —— 设置查看 / 更新 / 凭据写入。

- GET：库、收件箱、阈值、模型端点、auto_apply、定时、凭据掩码。
  凭据永远只返回掩码（前 2 + **** + 后 2），绝不返回明文。
- PUT：只更新 thresholds / auto_apply / schedule 回写 libraries.json；
  库路径不允许经 API 改（改路径须手动改文件），返回里注明。
- POST /key：key 写进项目 config/.env.local（600 权限，不存在则创建），
  同时写 os.environ。
"""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .deps import (ENV_LOCAL, LIBRARIES_JSON, get_settings, key_env_var,
                   load_libraries, mask_key, public_settings)

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsPutBody(BaseModel):
    thresholds: dict | None = None
    auto_apply: bool | None = None
    schedule: dict | None = None


class KeyBody(BaseModel):
    provider: str
    key: str


@router.get("")
def get():
    return public_settings()


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


@router.post("/key")
def set_key(body: KeyBody):
    provider = (body.provider or "").strip()
    key = (body.key or "").strip()
    if not provider:
        raise HTTPException(status_code=400, detail="provider 不能为空")
    if not key:
        raise HTTPException(status_code=400, detail="key 不能为空")
    env_var = key_env_var(provider)
    # 读现有 .env.local，替换或追加该行；其余行原样保留。
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
    return {"ok": True, "provider": provider, "env_var": env_var,
            "masked": mask_key(key),
            "note": "key 已写入 config/.env.local（600）并注入当前进程环境；GET 只返回掩码。"}
