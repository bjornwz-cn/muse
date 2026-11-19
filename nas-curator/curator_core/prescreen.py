#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prescreen —— 预筛层（架构 §3.5 / v3.1.1 之上的新增层）。

在规则打分之前，先用 bge-m3 向量快速判断文件是否“明显属于某类”：
文件名向量 ×0.45 + 正文向量 ×0.55 的融合相似度已在 semkit
SemanticClassifier.classify 内算好；本层只加一道更严格的门槛
（sim ≥ sim_min 且 margin ≥ margin_min，默认 0.75 / 0.10），
命中则直接定类，不再走规则/语义/LLM；未命中则不留痕迹，
不计入 llm_calls。

Prescreener 本身不调模型服务以外的任何东西；构造与 import
都不要求服务在线——服务只在 decide() 真正调用 classify() 时才碰。
"""
from __future__ import annotations

from .semkit import SemanticClassifier


class Prescreener:
    """包装 SemanticClassifier 的严格预筛器。"""

    def __init__(self, host="http://172.17.0.1:11500", model="bge-m3:latest",
                 sim_min=0.75, margin_min=0.10, sem=None):
        # sem 仅供注入已建好的分类器（测试/复用）；默认新建一个。
        self.sem = sem if sem is not None else SemanticClassifier(host, model)
        self.sim_min = sim_min
        self.margin_min = margin_min

    def fit(self, rules):
        """把规则集的类目向量装进底层分类器；返回类目向量数。"""
        return self.sem.fit(rules)

    def alive(self):
        """底层 embedding 服务是否可达。"""
        try:
            return bool(self.sem.alive())
        except Exception:
            return False

    def decide(self, name, text):
        """返回 (category or None, sim, margin)。

        仅当 sim ≥ sim_min 且 margin ≥ margin_min 时返回类别；
        否则返回 (None, sim, margin)。服务不可用/无类目向量时
        返回 (None, 0.0, 0.0)。reason 由调用方写为
        f"prescreen:{sim:.2f}"。
        """
        try:
            r = self.sem.classify(name or "", text or "")
        except Exception:
            return None, 0.0, 0.0
        if not r:
            return None, 0.0, 0.0
        sim = float(r.get("sim") or 0.0)
        margin = float(r.get("margin") or 0.0)
        if sim >= self.sim_min and margin >= self.margin_min:
            return r.get("category"), sim, margin
        return None, sim, margin
