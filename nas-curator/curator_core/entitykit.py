#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""entitykit —— 实体画像（把「谁的文件」变成可查的表）。

问题：内容里只有「北京信明启递科技发展有限公司 2015年度报告」，
      单看内容分不清这是自家资质还是供应商资质。
办法：从已整理库里统计「机构名 -> 常出现的类目」，
      新文件只要认出机构名，就能确定归属，不必猜。

输出结构（写进 rules.json 的 entities 字段）：
  {"北京信明启递科技发展有限公司": {"04-供应商与采购": 5, "01-公司资质与证照": 1}, ...}
判定时要求「主类目占比 >= 0.6 且出现 >= 2 次」，避免一次误伤就下结论。
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict

from . import orgkit


NOISE = ("打造", "大健康", "市场监督", "我们", "各公司", "本公司", "贵公司", "该公司",
         "有限公司有限", "二三级", "各级", "相关", "上述", "以下", "本年度")
GENERIC = {"人民医院", "中医院", "儿童医院", "老年医院", "助建医院", "中心医院",
           "第一医院", "第二医院", "第三医院", "医学院", "附属医院"}


def _ok(name, files):
    if re.match(r"^[\d０-９]", name):
        return False
    if not (4 <= len(name) <= 30):
        return False
    if name in GENERIC or any(w in name for w in NOISE):
        return False
    return files >= 2


def build(samples_by_cat, self_names=(), min_files=2):
    """samples_by_cat: {类目: [正文或文件名文本,...]}"""
    table = defaultdict(Counter)
    for cat, texts in samples_by_cat.items():
        for t in texts:
            if not t:
                continue
            for o in set(orgkit.orgs(t, 8)):
                table[o][cat] += 1
    out = {}
    for org, c in table.items():
        total = sum(c.values())
        if not _ok(org, total) or total < min_files:
            continue
        cat, n = c.most_common(1)[0]
        if n / total >= 0.6:
            out[org] = {cat: n, "__total": total, "__ratio": round(n / total, 2)}
    return out


def match(text, table, self_names=(), allow_self=True):
    """返回 (类目, 依据) 或 (None, "")。"""
    if not text or not table:
        return None, ""
    hits = Counter()
    why = []
    for o in orgkit.orgs(text, 8):
        info = table.get(o)
        if not info:
            continue
        is_self = any(s and (s in o or o in s) for s in self_names)
        if is_self and not allow_self:
            continue
        for cat, n in info.items():
            if cat.startswith("__"):
                continue
            hits[cat] += n
            why.append(f"{o}→{cat}")
    if not hits:
        return None, ""
    cat, n = hits.most_common(1)[0]
    total = sum(hits.values())
    if n / total < 0.6 or n < 1:
        return None, ""
    return cat, "实体:" + "；".join(why[:2])


def merge_into_rules(path, rules, samples_by_cat):
    ents = build(samples_by_cat, rules.get("self_names") or ())
    rules["entities"] = ents
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=1)
    return len(ents)


if __name__ == "__main__":
    import sys
    rules = json.load(open(sys.argv[1], encoding="utf-8"))
    print("实体数:", len(rules.get("entities") or {}))
    for k, v in list((rules.get("entities") or {}).items())[:8]:
        print(" ", k, v)
