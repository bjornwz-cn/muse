#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""media_core —— NAS Curator 媒体引擎（P1）。

从 media-library-curator 的 curate.py 模块化而来：判定逻辑与阈值未改，
只做模块化拆分 + 统一 plan-item 输出 + 视频多帧采样。

import 顺序坑（cv2 必须先于 torch）：
NAS 的 marvis runtime python 直接 ``import torch`` 会报
``libopenblasp-*.so 缺失``，因为 torch 依赖的 OpenBLAS 只在 opencv 的
``.libs`` 里；原技能能跑是因为 decode 流程先 import 了 cv2（其 .libs 被
加载进进程），之后 torch 才能 import。本包把这个隐式依赖显式化：
唯一可能触发 torch 加载的路径是 ``Classifier`` 实例化，其 ``__init__``
先调 ``ensure_cv2_first()``（先 import cv2 再 import torch），
见 :mod:`media_core.curate` 的模块 docstring 与该函数。
本包 import 本身绝不 import torch/cv2、不加载模型；模型只在
``Classifier`` 实例化（或 :func:`media_core.engine.build_classifier`）
时加载。

导出：
  plan_media_files / plan_inbox / build_classifier（engine 层），
  Classifier / decide / rebuild_index（curate 层），
  PHOTO_CATEGORIES / VIDEO_CATEGORIES（12 类照片 / 6 类视频），
  MEDIA_EXTS（照片+视频扩展名全集，与 is_media() 判定完全一致，
  统一 CLI 用它收集媒体文件路径），
  IMG_EXT / VID_EXT / ensure_cv2_first。
"""
from __future__ import annotations

from .curate import (
    IMG_EXT,
    MEDIA_EXTS,
    VID_EXT,
    PHOTO_CATEGORIES,
    VIDEO_CATEGORIES,
    Classifier,
    decide,
    ensure_cv2_first,
    get_image_exts,
    media_exts,
    rebuild_index,
)
from .engine import build_classifier, plan_inbox, plan_media_files

__all__ = [
    "plan_media_files",
    "plan_inbox",
    "build_classifier",
    "Classifier",
    "decide",
    "rebuild_index",
    "PHOTO_CATEGORIES",
    "VIDEO_CATEGORIES",
    "MEDIA_EXTS",
    "IMG_EXT",
    "VID_EXT",
    "get_image_exts",
    "media_exts",
    "ensure_cv2_first",
]
