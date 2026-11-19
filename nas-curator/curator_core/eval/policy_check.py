#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""策略单元验证：无正文/机器名是否真的不猜；可读名无正文是否压低置信；有正文是否正常判。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
from curator_core.llmkit import LLM

HERE = os.path.dirname(os.path.abspath(__file__))
rules = json.load(open(os.path.join(HERE, "rules_v8.json"), encoding="utf-8"))
cats = rules["categories"]

# (文件名, 正文, 期望动作, 期望类目)
CASES = [
    ("46805edecfe16ad98e2421d2b4e169ce.png", "", "skip", None),
    ("1B23C58F-B675-480B-9905-C825D7966424.png", "", "skip", None),
    ("IMG_20220513_141233.jpg", "", "skip", None),
    ("20220115.pdf", "", "skip", None),
    ("医疗器械经营许可证-扫描件.pdf", "", "low", None),
    ("2023年3月利润表.pdf", "（图片，无文字层）", "low", None),
    ("房屋租赁合同-已签署.docx", "", "low", None),
    ("陈松沪简历.pdf", "", "low", None),
    ("增值税及附加税费申报表", "纳税人识别号 9161000068799096X0 税款所属期间：2021-11-01 至 2021-11-30"
                              " 增值税及附加税费申报表（一般纳税人适用） 销售额 应纳税额 附加税费 申报日期 2021-12-13",
     "ok", "07-财务与经营分析"),
    ("陈松沪简历.pdf", "陈松沪 男 25岁 意向岗位：器械销售 教育经历 2014.9-2017.6 连云港师范高等专科学校"
                      " 工作经历 2020.1-至今 青海钐纺医疗器械有限公司 器械销售 负责区域销售与客户维护",
     "ok", "11-人事与行政"),
    ("螺钉验收标准", "螺钉验收标准 致：华润（北京）医疗器械供应链管理有限公司 我司验收创伤螺钉标准如下："
                    "产品表面无使用痕迹，产品无物理损伤，无折断，以螺钉规格与合格证规格相符作为验收合格标准",
     "ok", "06-库存与物流|04-供应商与采购|14-工具与其他"),
    ("辽阳营口招标降价策略", "1、贝朗 降价 输液器在辽阳带量价格上浮30%，防逆流留置针降价40% "
                            "2、强生 降价 3、泰尔茂 不投 4、保赫曼 不降价（或放弃投标） 对于本次招标认为是未来辽宁省招标降价先锋",
     "ok", "12-招投标"),
]

llm = LLM(timeout=90)
res = llm.classify_batch([(c[0], c[1]) for c in CASES], cats, chunk=6, min_text=20)
ok = 0
print(f"{'期望':6s}{'结果':6s}{'置信':>7s}{'类目/原因':28s}文件")
for (fn, _t, want, want_cat), r in zip(CASES, res):
    if not r:
        got, conf, cat = "fail", 0, "调用失败"
    elif r.get("skipped"):
        got, conf, cat = "skip", 0, "skipped:" + r.get("reason", "")
    else:
        conf = r["confidence"]
        cat = r["category"]
        got = "low" if r.get("low_text") else "ok"
    if want == "skip":
        good = got == "skip"
    elif want == "low":
        good = got in ("low",) and conf <= 0.45
    else:
        accept = str(want_cat).split("|")
        good = cat in accept
    ok += bool(good)
    print(f"{want:6s}{('PASS' if good else 'FAIL'):6s}{conf:>7.2f} {str(cat)[:26]:28s}{fn[:40]}")
print(f"\n通过 {ok}/{len(CASES)} | {llm.report()}")
json.dump({"cases": [{"name": c[0], "want": c[2], "want_cat": c[3], "got": r} for c, r in zip(CASES, res)],
           "pass": ok, "total": len(CASES)},
          open(os.path.join(HERE, "cache", "policy_check.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
