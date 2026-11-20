#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""common.embedding —— 向量模型配置（0.1.5）：语义预筛 / 聚类用的 embedding 模型。

背景：向量模型原来写死 bge-m3:latest（三处：api/jobs.py、curator.py
--sem-model 默认值、curator_core/llmkit.py DEFAULT_EMBED），
且与语言模型列表混在一起容易误配（0.1.5 前用户曾把豆包 embedding 模型
加进语言调用链——embedding 模型做不了对话，调用会失败后跳过）。
0.1.5 起向量模型独立配置：endpoint（即 SEM_HOST，环境变量优先，只读）
+ 模型（libraries.json model_endpoints.embedding_model，可配）；
本地 Ollama，无需 Key；向量模型不进语言调用链。

配置读取按文件 mtime 缓存；读不到/非法时回缺省，绝不抛错。
本模块只依赖标准库。
"""
from __future__ import annotations

import json
import os
import re

DEFAULT_EMBEDDING_MODEL = "bge-m3:latest"
DEFAULT_EMBEDDING_HOST = "http://172.17.0.1:11500"

# 模型名：字母数字开头，可含 . _ : / -（如 bge-m3:latest、org/model:tag）
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")

_cache = {"path": None, "mtime_ns": None, "size": None, "value": None}


def _config_dir() -> str:
    """配置目录：与 api.deps.CONFIG_DIR 同口径（环境变量优先）。"""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.environ.get("CURATOR_CONFIG_DIR") or os.path.join(here, "config")


def _libraries_path() -> str:
    return os.path.join(_config_dir(), "libraries.json")


def validate_embedding_model(value) -> str:
    """校验向量模型名（设置页 PUT 用）：非空、无空白、长度 ≤128。非法抛 ValueError。"""
    if not isinstance(value, str):
        raise ValueError("向量模型名须为字符串")
    model = value.strip()
    if not model:
        raise ValueError("向量模型名不能为空")
    if not _MODEL_RE.match(model):
        raise ValueError(
            f"向量模型名 {value!r} 不合法：须为字母数字开头，可含 . _ : / -，"
            "如 bge-m3:latest")
    return model


def clear_cache() -> None:
    """清缓存（测试用；设置页 PUT 写回后也会调）。"""
    _cache.update({"path": None, "mtime_ns": None, "size": None, "value": None})


def _read_configured() -> dict | None:
    """读 libraries.json 的 model_endpoints；缺失/非法返回 None（用缺省）。"""
    path = _libraries_path()
    try:
        st = os.stat(path)
    except OSError:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return None
    me = cfg.get("model_endpoints") if isinstance(cfg, dict) else None
    if not isinstance(me, dict):
        return None
    return me


def load_embedding_config() -> dict:
    """当前生效的向量模型配置 {"host": ..., "model": ...}（mtime 缓存）。

    host：环境变量 SEM_HOST 优先（与 get_settings 同口径），其次
    libraries.json model_endpoints.sem_host，最后缺省；
    model：libraries.json model_endpoints.embedding_model，非法/缺失回缺省。
    """
    path = _libraries_path()
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        key = (path, None, None)
    if (_cache["path"], _cache["mtime_ns"], _cache["size"]) == key \
            and _cache["value"] is not None:
        return dict(_cache["value"])
    me = _read_configured() or {}
    host = (os.environ.get("SEM_HOST") or me.get("sem_host") or
            DEFAULT_EMBEDDING_HOST)
    try:
        model = validate_embedding_model(me.get("embedding_model") or "")
    except ValueError:
        model = DEFAULT_EMBEDDING_MODEL
    value = {"host": host, "model": model}
    _cache.update({"path": key[0], "mtime_ns": key[1], "size": key[2],
                   "value": value})
    return dict(value)
