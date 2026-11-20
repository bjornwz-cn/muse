#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""common.llm_models —— 模型列表（0.1.2）：用户可配的 LLM 优先级链。

存 config/llm_models.json（CONFIG_DIR 下，容器内 /data/config/llm_models.json）。
本文件只记模型元数据（名称/类型/endpoint 或模型 ID/Key 变量名/启用/顺序），
绝不含 Key 明文：Key 只存 config/.env.local（600），由设置页写入，
llmkit 按 key_env 变量名从环境变量或 .env.local 取值。

默认列表（文件不存在时）保留现有行为：
  火山 Ark Plan（Key 为空时跳过）→ 火山 Ark 按量 → 本地 Ollama（兜底）。
文件一旦存在（用户在设置页保存过），即按文件内容原样执行，不再强制补本地兜底。
"""
from __future__ import annotations

import copy
import json
import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MODEL_TYPES = ("ark-plan", "ark-payg", "ollama", "custom")
TYPE_LABELS = {"ark-plan": "火山 Ark Plan", "ark-payg": "火山 Ark 按量",
               "ollama": "本地 Ollama", "custom": "自定义 OpenAI 兼容"}

ARK_BASE_PLAN = "https://ark.cn-beijing.volces.com/api/plan/v3"
ARK_BASE_PAYG = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_ARK_MODEL = "deepseek-v4-1-flash-261017"
DEFAULT_OLLAMA_HOST = "http://172.17.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen3:4b"
DEFAULT_EMBED_MODEL = "bge-m3:latest"

DEFAULT_KEY_ENV = {"ark-plan": "ARK_PLAN_KEY", "ark-payg": "ARK_PAYG_KEY"}

DEFAULT_MODELS = [
    {"id": "ark-plan", "name": "火山 Ark Plan", "type": "ark-plan",
     "model": DEFAULT_ARK_MODEL, "base_url": ARK_BASE_PLAN,
     "key_env": "ARK_PLAN_KEY", "enabled": True},
    {"id": "ark-payg", "name": "火山 Ark 按量", "type": "ark-payg",
     "model": DEFAULT_ARK_MODEL, "base_url": ARK_BASE_PAYG,
     "key_env": "ARK_PAYG_KEY", "enabled": True},
    {"id": "local", "name": "本地 Ollama", "type": "ollama",
     "model": DEFAULT_OLLAMA_MODEL, "base_url": DEFAULT_OLLAMA_HOST,
     "embed_model": DEFAULT_EMBED_MODEL, "key_env": "", "enabled": True},
]

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def mask_key(value: str) -> str:
    """掩码：前 2 + **** + 后 2；长度 ≤4 全掩码；空值返回空串。
    与 api/deps.py mask_key 同口径（本模块为规范出处，deps 引用此处）。"""
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return f"{value[:2]}****{value[-2:]}"


def models_path() -> str:
    """模型列表文件路径：CURATOR_LLM_MODELS > CURATOR_CONFIG_DIR > 项目 config/。"""
    env = os.environ.get("CURATOR_LLM_MODELS")
    if env:
        return env
    cfg_dir = os.environ.get("CURATOR_CONFIG_DIR")
    if cfg_dir:
        return os.path.join(cfg_dir, "llm_models.json")
    return os.path.join(PROJECT_ROOT, "config", "llm_models.json")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s  # 纯中文名时为空，调用方用 model-<序号> 兜底


def validate_models(models) -> list:
    """校验并归一化模型列表（不含 Key，明文 Key 在此直接拒绝）。

    返回归一化后的新列表；非法时抛 ValueError（附原因）。
    """
    if not isinstance(models, list):
        raise ValueError("models 须为列表")
    if len(models) > 20:
        raise ValueError("模型最多 20 个")
    out = []
    seen_ids = set()
    for i, m in enumerate(models):
        if not isinstance(m, dict):
            raise ValueError(f"第 {i + 1} 个模型须为对象")
        if "api_key" in m or "key" in m:
            raise ValueError(f"模型 {m.get('name') or i + 1}：Key 不得写入模型列表文件")
        mtype = (m.get("type") or "").strip()
        if mtype not in MODEL_TYPES:
            raise ValueError(
                f"模型 {m.get('name') or i + 1}：type 须为 "
                + " / ".join(MODEL_TYPES))
        name = (m.get("name") or "").strip()
        if not name:
            raise ValueError(f"第 {i + 1} 个模型：name 不能为空")
        mid = (m.get("id") or "").strip()
        if not mid:
            # 纯中文名 slug 为空时用 model-<序号> 兜底，避免撞 id
            mid = _slug(name) or f"model-{i + 1}"
        if not _ID_RE.match(mid):
            raise ValueError(f"模型 {name}：id 只允许小写字母/数字/_/-")
        if mid in seen_ids:
            raise ValueError(f"模型 id 重复：{mid}")
        seen_ids.add(mid)
        model = (m.get("model") or "").strip()
        if not model:
            raise ValueError(f"模型 {name}：model（模型 ID）不能为空")
        base_url = (m.get("base_url") or "").strip()
        if mtype == "ark-plan":
            base_url = base_url or ARK_BASE_PLAN
        elif mtype == "ark-payg":
            base_url = base_url or ARK_BASE_PAYG
        elif mtype == "ollama":
            base_url = base_url or DEFAULT_OLLAMA_HOST
        elif not base_url:
            raise ValueError(f"模型 {name}：自定义类型须填 endpoint（base_url）")
        key_env = (m.get("key_env") or "").strip()
        if mtype in ("ark-plan", "ark-payg"):
            key_env = key_env or DEFAULT_KEY_ENV[mtype]
        elif mtype == "custom":
            key_env = key_env or ("LLM_KEY_" + mid.upper().replace("-", "_"))
        else:  # ollama 不用 Key
            key_env = ""
        if key_env and not _ENV_RE.match(key_env):
            raise ValueError(f"模型 {name}：key_env 只允许大写字母/数字/_")
        timeout = m.get("timeout")
        if timeout is not None:
            try:
                timeout = int(timeout)
            except (TypeError, ValueError):
                raise ValueError(f"模型 {name}：timeout 须为秒数")
            if timeout <= 0:
                raise ValueError(f"模型 {name}：timeout 须大于 0")
        item = {"id": mid, "name": name, "type": mtype, "model": model,
                "base_url": base_url, "key_env": key_env,
                "enabled": bool(m.get("enabled", True))}
        if timeout is not None:
            item["timeout"] = timeout
        if mtype == "ollama" and (m.get("embed_model") or "").strip():
            item["embed_model"] = (m.get("embed_model") or "").strip()
        out.append(item)
    return out


def load_models() -> tuple:
    """返回 (models, source)：source 为 'file'（文件存在且合法）或 'default'。

    文件缺失/损坏时返回默认列表（不抛错，保证分类流程不阻塞）。
    """
    path = models_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            raw = data.get("models") if isinstance(data, dict) else data
            return validate_models(raw), "file"
        except (OSError, ValueError):
            return copy.deepcopy(DEFAULT_MODELS), "default"
    return copy.deepcopy(DEFAULT_MODELS), "default"


def save_models(models) -> list:
    """校验并写回模型列表文件（只写元数据，绝不含 Key）。返回归一化后的列表。"""
    norm = validate_models(models)
    path = models_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "models": norm}, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return norm


def public_models(models, key_values: dict) -> list:
    """给设置页 GET 用：每条补 key_set / key_masked，绝不返回 Key 明文。"""
    out = []
    for m in models:
        d = dict(m)
        env_var = m.get("key_env") or ""
        value = (key_values or {}).get(env_var, "") if env_var else ""
        d["key_set"] = bool(value)
        d["key_masked"] = mask_key(value) if value else ""
        out.append(d)
    return out
