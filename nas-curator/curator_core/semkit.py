#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""semkit —— 语义向量分类（bge-m3 via ollama）。

为什么需要它：关键词规则只认字面，看不懂「这份文件在说什么」；
LLM 逐条判又慢。向量方案把「类目」和「文件」都映射到同一语义空间，
用余弦相似度归类：毫秒级、可解释、对改写/同义说法鲁棒。

流程：
  1) 每个类目取若干代表样本（examples）-> 平均成类目向量；
  2) 文件正文（或文件名）-> 向量；
  3) 取最相似类目；最高分过低或与第二名太接近 -> 判低置信，交人工/LLM。
"""
from __future__ import annotations

import json
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import requests


class Embedder:
    def __init__(self, host="http://172.17.0.1:11500", model="bge-m3:latest",
                 timeout=60, workers=8):
        self.url = host.rstrip("/") + "/api/embeddings"
        self.model = model
        self.timeout = timeout
        self.workers = workers
        self._lock = threading.Lock()

    def alive(self):
        try:
            r = requests.post(self.url, json={"model": self.model, "prompt": "测试"},
                              timeout=self.timeout)
            return r.status_code == 200 and "embedding" in r.text
        except Exception:
            return False

    def one(self, text):
        text = (text or "").strip()
        if not text:
            return None
        # bge-m3 上下文 8k，这里截断到 1200 字足够判类
        try:
            r = requests.post(self.url, json={"model": self.model, "prompt": text[:1200]},
                              timeout=self.timeout)
            if r.status_code == 200:
                return r.json().get("embedding")
        except Exception:
            pass
        return None

    def many(self, texts):
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            return list(ex.map(self.one, texts))


def cosine(a, b):
    if not a or not b:
        return 0.0
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return s / (na * nb) if na and nb else 0.0


def mean_vec(vecs):
    vecs = [v for v in vecs if v]
    if not vecs:
        return None
    n = len(vecs)
    d = len(vecs[0])
    out = [0.0] * d
    for v in vecs:
        for i, x in enumerate(v[:d]):
            out[i] += x
    return [x / n for x in out]


def build_centroids(embedder, rules, max_examples=6, self_names=()):
    """为每个类目算中心向量：类目名+描述+代表样本 一起平均。"""
    cats = rules.get("categories", [])
    texts, owners = [], []
    from . import orgkit
    for i, c in enumerate(cats):
        base = c["path"] + ("：" + c["desc"] if c.get("desc") else "")
        texts.append(base)
        owners.append(i)
        for ex in (c.get("examples") or [])[:max_examples]:
            ex = str(ex).strip()
            if len(ex) >= 8:
                lb = orgkit.label(ex, self_names)
                texts.append((lb + "\n" if lb else "") + ex[:500])
                owners.append(i)
    vecs = embedder.many(texts)
    buckets = {}
    for v, o in zip(vecs, owners):
        if v:
            buckets.setdefault(o, []).append(v)
    cents = {}
    for i, c in enumerate(cats):
        mv = mean_vec(buckets.get(i, []))
        if mv:
            cents[c["path"]] = mv
    return cents


# 文件名是人工命名，信噪比比正文高；这些词只要出现在文件名里，就给对应类目加一点分。
# 只加分不拍板，避免「合同台账」这类跨类目误伤。
HINTS = {
    "发票": "财务", "利润": "财务", "报表": "财务", "凭证": "财务", "报税": "财务",
    "合同": "合同", "协议": "合同",
    "简历": "人事", "劳动合同": "人事", "社保": "人事", "考勤": "人事",
    "周报": "周报", "工作总结": "周报", "月报": "周报",
    "招标": "招投标", "中标": "招投标", "挂网": "招投标", "集采": "招投标", "报价": "招投标",
    "注册证": "注册", "说明书": "产品", "授权书": "产品",
    "营业执照": "资质", "许可证": "资质", "资质": "资质",
    "库存": "库存", "物流": "库存", "验收": "库存",
    "会议": "会议", "纪要": "会议", "例会": "会议",
    "年审": "人事", "申报": "财务", "税": "财务", "工资": "人事", "薪酬": "人事",
    "供应商": "供应商", "采购": "供应商",
    "报量": "销售", "销量": "销售", "销售": "销售",
    "方案": "运营", "尽调": "运营", "项目": "运营",
}
HINT_BONUS = 0.06


class SemanticClassifier:
    def __init__(self, host="http://172.17.0.1:11500", model="bge-m3:latest",
                 timeout=60, workers=8, min_sim=0.42, min_margin=0.02, self_names=()):
        self.emb = Embedder(host, model, timeout, workers)
        self.cents = {}
        self.min_sim = min_sim
        self.min_margin = min_margin
        self.self_names = list(self_names or [])
        self.entities = {}

    def alive(self):
        return self.emb.alive()

    def fit(self, rules):
        if not self.self_names:
            self.self_names = list(rules.get("self_names") or [])
        self.entities = rules.get("entities") or {}
        self.cents = build_centroids(self.emb, rules, self_names=self.self_names)
        return len(self.cents)

    def _hint(self, name, cat):
        name = name or ""
        for kw, target in HINTS.items():
            if kw in name and target in cat:
                return HINT_BONUS
        return 0.0

    def _best(self, nv, tv, name):
        """文件名向量与正文向量分开算再融合：文件名权重略低但独立成信号。"""
        scores = []
        for cat, cv in self.cents.items():
            ns = cosine(nv, cv) if nv else 0.0
            ts = cosine(tv, cv) if tv else 0.0
            s = ns * 0.45 + ts * 0.55 if (nv and tv) else max(ns, ts)
            scores.append((s + self._hint(name, cat), cat))
        scores.sort(reverse=True)
        if not scores:
            return None
        top = scores[0]
        second = scores[1] if len(scores) > 1 else (0.0, "")
        margin = top[0] - second[0]
        # 相似度足够高可以单独成立；否则要求与第二名有明确差距
        ok = top[0] >= self.min_sim and (top[0] >= 0.62 or margin >= 0.05)
        return {"category": top[1], "sim": round(top[0], 4),
                "margin": round(margin, 4), "second": second[1], "ok": ok}

    def classify(self, name, text):
        # 0) 实体命中优先：认得这个机构，归属就确定，不用猜
        if self.entities:
            from . import entitykit
            cat, why = entitykit.match(f"{name} {text}", self.entities, self.self_names)
            if cat:
                return {"category": cat, "sim": 1.0, "margin": 1.0,
                        "second": "", "ok": True, "via": why}
        # 正文和文件名都要：正文说明内容，文件名常有类别词；再叠加主体标签
        from . import orgkit
        lb = orgkit.label(text, self.self_names)
        parts = [x for x in (lb, (text or "")[:1200]) if x]
        nv = self.emb.one(name or "")
        tv = self.emb.one("\n".join(parts)) if parts else None
        r = self._best(nv, tv, name)
        # 图库类兜底：纯图片且抽不出正文，内容无从判断，直接归影像类，不算猜
        ext = os.path.splitext(name or "")[1].lower()
        if r and not tv and ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"):
            for cat in self.cents:
                if "图片" in cat or "影像" in cat or "照片" in cat:
                    return {"category": cat, "sim": 1.0, "margin": 1.0, "second": "",
                            "ok": True, "via": "纯图片"}
            r["ok"] = False
        elif r and not tv:
            # 没有正文、也不是图片：只靠文件名，不下强结论
            r["ok"] = False
        return r

    def classify_batch(self, items):
        """items=[(name, text)]，并发算。"""
        def work(it):
            name, text = it
            return self.classify(name, text)
        with ThreadPoolExecutor(max_workers=self.emb.workers) as ex:
            return list(ex.map(work, items))


def save_rules_with_examples(path, rules, examples_by_cat):
    for c in rules["categories"]:
        ex = examples_by_cat.get(c["path"])
        if ex:
            c["examples"] = ex[:6]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=1)
    return path


if __name__ == "__main__":
    import sys
    e = Embedder()
    print("alive:", e.alive())
    vs = e.many(["医疗器械注册证", "医院供货合同", "周报"])
    print([len(v) if v else None for v in vs])
    if all(vs):
        print("注册证-合同", round(cosine(vs[0], vs[1]), 3),
              "注册证-周报", round(cosine(vs[0], vs[2]), 3))
