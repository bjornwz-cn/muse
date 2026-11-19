#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""curator_core —— 文档整理引擎（workfiles-curator v3.1.1 包化）。

import 本包不要求任何模型服务在线；LLM/语义/预筛只在调用方
传入实例时才用。
"""
from .engine import (
    apply_plan,
    best_category,
    explain,
    inventory,
    learn_rules,
    plan_files,
)
from .llmkit import LLM
from .prescreen import Prescreener
from .semkit import SemanticClassifier
from .textkit import extract_text

__all__ = [
    "best_category",
    "plan_files",
    "apply_plan",
    "inventory",
    "learn_rules",
    "explain",
    "Prescreener",
    "SemanticClassifier",
    "LLM",
    "extract_text",
]
