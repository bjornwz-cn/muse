#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""orgkit —— 主体识别。

为什么要它：同一类文件，主体不同归属就不同。
  · 「营业执照」是本公司的 -> 公司资质
  · 「营业执照」是供货方的 -> 供应商资质
  · 「合作协议」签给医院 -> 医院合作项目
只按内容分类会把三者混为一谈，识别主体后语义空间自然分开。
"""
from __future__ import annotations

import re
from collections import Counter

_SUFFIX = r"(?:有限责任公司|股份有限公司|有限公司|集团公司|集团|医院|中医院|人民医院|大学|学院|研究院|研究所|事务所|门诊部|药房|医药公司|器械公司)"
ORG_RE = re.compile(r"(?:^|[\s：:，,。；;、（(\n])([\u4e00-\u9fa5A-Za-z0-9·]{2,20}?" + _SUFFIX + ")")
# 「XX医院」这类常被前文动词带来的杂字污染，做一次清洗
HEAD_CLEAN = re.compile(r"^(?:与|和|向|由|对|给|至|及|的|了|在|为|是|就|将|把|经|据|本|该|甲乙|双方|甲方|乙方|丙方|关于|拟与|拟向|覆盖|其中|清单|载明|确认|联系|签订|指定)+")
BAD_WORDS = ("载明", "覆盖", "其中", "清单", "拟与", "关于", "各家", "以上", "如下")


def orgs(text, limit=10):
    """抽出正文里的机构名（去重保序）。"""
    if not text:
        return []
    out, seen = [], set()
    for m in ORG_RE.finditer(text):
        s = HEAD_CLEAN.sub("", m.group(1)).strip("（）()·")
        if len(s) < 4 or any(w in s for w in BAD_WORDS):
            continue
        key = s.replace("（", "(").replace("）", ")")
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def learn_self_names(texts, top=2):
    """从一堆正文里推断「本公司」：出现最频繁、跨文件覆盖面最广的那个。

    用的是覆盖率（出现在多少个文件里）而不是总频次，
    因为本公司名会在各类文件里反复出现，供应商只在采购类出现。
    """
    cover = Counter()
    for t in texts:
        for o in set(orgs(t, 6)):
            cover[o] += 1
    return [o for o, _ in cover.most_common(top)]


def label(text, self_names=()):
    """给语义向量用的主体标签串。"""
    found = orgs(text, 6)
    if not found:
        return ""
    tags = []
    selfs = [s for s in self_names if s]
    is_self = any(s in (text or "") for s in selfs)
    others = [o for o in found if not any(s and (s in o or o in s) for s in selfs)]
    if is_self:
        tags.append("本方主体")
    if others:
        kinds = []
        for o in others[:4]:
            if o.endswith(("医院", "中医院", "人民医院", "门诊部", "药房")):
                kinds.append("医院:" + o)
            elif o.endswith(("大学", "学院", "研究院", "研究所")):
                kinds.append("院校:" + o)
            else:
                kinds.append("企业:" + o)
        tags.append("其他主体 " + "、".join(kinds))
    return "[主体] " + "；".join(tags) if tags else ""


if __name__ == "__main__":
    t = ("甲方：中航工业西安医院  乙方：陕西纳威医疗器械有限公司\n"
         "按照《陕西省深化医药卫生体制改革试点方案》，由陕西纳威医疗器械有限公司供货…")
    print(orgs(t))
    print(learn_self_names([t, "陕西纳威医疗器械有限公司 营业执照", "西安红会医院 合同 陕西纳威医疗器械有限公司"]))
    print(label(t, ["陕西纳威医疗器械有限公司"]))
