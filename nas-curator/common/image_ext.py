#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""common.image_ext —— 图片扩展名配置（0.1.5）：文档引擎与媒体引擎共用同一份配置。

背景：图片扩展名原来在两处硬编码，且两处不一致——
curator_core/textkit.py 10 个（.jpg/.jpeg/.png/.bmp/.tif/.tiff/.webp/.gif/.heic/.heif），
media_core/curate.py 16 个（另含 .jfif/.nef/.cr2/.arw/.dng/.livp）。
缺省取两处并集（16 个），保证行为不丢；用户可在设置页删减。

配置存 config/libraries.json 顶层 image_extensions（列表）。
读取按文件 mtime 缓存：设置页 PUT 写回后，下轮扫描即生效，无需重启容器。
本模块只依赖标准库；读取失败时一律回缺省，绝不抛错（引擎热路径调用）。
"""
from __future__ import annotations

import json
import os
import re

# 两处旧硬编码列表的并集（缺省）：文档引擎 10 个 + 媒体引擎多出的 6 个
# （.jfif/.nef/.cr2/.arw/.dng/.livp，相机 raw 与 JFIF 照片）。
DEFAULT_IMAGE_EXTS = frozenset({
    ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp", ".gif",
    ".heic", ".heif", ".jfif", ".nef", ".cr2", ".arw", ".dng", ".livp",
})

_EXT_RE = re.compile(r"^\.[a-z0-9]{1,10}$")
MAX_EXTS = 50

_cache = {"path": None, "mtime_ns": None, "size": None, "value": None}


def _config_dir() -> str:
    """配置目录：与 api.deps.CONFIG_DIR 同口径（环境变量优先）。"""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.environ.get("CURATOR_CONFIG_DIR") or os.path.join(here, "config")


def _libraries_path() -> str:
    return os.path.join(_config_dir(), "libraries.json")


def validate_image_exts(value) -> list:
    """校验并归一化扩展名列表（设置页 PUT 用）。

    接受 list/tuple；每项 strip 后转小写、缺点的补点；去重保序。
    非法时抛 ValueError（附原因）。
    """
    if not isinstance(value, (list, tuple)):
        raise ValueError("image_extensions 须为列表")
    if not value:
        raise ValueError("image_extensions 不能为空（至少保留一个图片扩展名）")
    if len(value) > MAX_EXTS:
        raise ValueError(f"图片扩展名最多 {MAX_EXTS} 个")
    out = []
    seen = set()
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(f"第 {i + 1} 个扩展名须为字符串")
        ext = item.strip().lower()
        if not ext:
            raise ValueError(f"第 {i + 1} 个扩展名不能为空")
        if not ext.startswith("."):
            ext = "." + ext
        if not _EXT_RE.match(ext):
            raise ValueError(
                f"扩展名 {item!r} 不合法：须为小写字母/数字，形如 .jpg（1–10 字符）")
        if ext in seen:
            continue
        seen.add(ext)
        out.append(ext)
    if not out:
        raise ValueError("image_extensions 不能为空（至少保留一个图片扩展名）")
    return out


def clear_cache() -> None:
    """清缓存（测试用；设置页 PUT 写回后也会调）。"""
    _cache.update({"path": None, "mtime_ns": None, "size": None, "value": None})


def _read_configured() -> frozenset | None:
    """读 libraries.json 的 image_extensions；缺失/非法返回 None（用缺省）。"""
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
    raw = cfg.get("image_extensions") if isinstance(cfg, dict) else None
    if raw is None:
        return None
    try:
        return frozenset(validate_image_exts(raw))
    except ValueError:
        return None


def get_image_exts() -> frozenset:
    """当前生效的图片扩展名集合（mtime 缓存；读不到/非法时回缺省）。"""
    path = _libraries_path()
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        key = (path, None, None)
    if (_cache["path"], _cache["mtime_ns"], _cache["size"]) == key \
            and _cache["value"] is not None:
        return _cache["value"]
    value = _read_configured()
    if value is None:
        value = DEFAULT_IMAGE_EXTS
    _cache.update({"path": key[0], "mtime_ns": key[1], "size": key[2],
                   "value": value})
    return value
