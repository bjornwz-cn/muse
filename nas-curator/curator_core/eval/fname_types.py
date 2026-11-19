#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文件名业务类型词表（快速通道）原型 + 在 152 条审计样本上的验证。

只读，不改产品代码。命中即定类，精确率必须 ≥90% 才允许进生产。
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

HERE = os.path.dirname(os.path.abspath(__file__))
rows = json.load(open(os.path.join(HERE, "cache", "audit_rows.json"), encoding="utf-8"))

# 有序：先匹配先赢。容器型（合同/协议）优先于单据型（发票/申报表），再业务型。
FILENAME_TYPES = [
    ("10-周报与工作总结", ["周报", "日报", "月报", "季报", "工作总结", "年度总结", "半年总结", "工作计划表"]),
    ("09-会议与汇报", ["会议纪要", "例会", "沟通函", "汇报材料", "汇报ppt", "阶段性汇报", "工作会议", "会议记录"]),
    ("11-人事与行政", ["简历", "花名册", "离职证明", "离职申请", "入职", "组织架构", "考勤", "人员名单", "人员分工", "员工"]),
    ("01-公司资质与证照", ["营业执照", "经营许可证", "资质证书", "许可证", "备案凭证", "药监", "信用报告", "开户许可证"]),
    ("03-合同与协议", ["合同", "协议", "契约", "框架协议", "补充协议", "订单合同", "承诺函", "授权书", "委托书"]),
    ("07-财务与经营分析", ["发票", "申报表", "纳税申报", "完税", "财务报表", "利润表", "折旧", "凭证", "账簿", "税", "审计报告"]),
    ("12-招投标", ["招标", "投标", "中标", "挂网", "议价", "集采", "带量采购"]),
    ("04-供应商与采购", ["供应商", "采购", "报价单", "报价表", "账单", "结算单", "对账", "分仓", "授权省份"]),
    ("06-库存与物流", ["库存", "物流", "配送", "仓储", "运输", "交接单", "出入库"]),
    ("02-产品与注册", ["产品手册", "说明书", "产品介绍", "标签", "产品注册", "技术参数"]),
    ("13-图片与影像", ["照片", "截图", "录屏", "录音", "影像", "图片"]),
    ("05-销售与业务数据", ["销售", "业绩", "回款", "商业计划", "财务测算", "业务简介", "销售激励", "分销"]),
    ("08-运营与项目", ["运营", "项目方案", "项目计划", "合作方案", "调研", "立项"]),
]
EXT_TYPES = {".jpg": "13-图片与影像", ".jpeg": "13-图片与影像", ".png": "13-图片与影像",
             ".gif": "13-图片与影像", ".bmp": "13-图片与影像", ".mp4": "13-图片与影像",
             ".mov": "13-图片与影像", ".mp3": "13-图片与影像", ".m4a": "13-图片与影像",
             ".wav": "13-图片与影像"}


def by_filename(name, ext):
    stem = os.path.splitext(name)[0].lower()
    hits = []
    for cat, words in FILENAME_TYPES:
        for w in words:
            if w.lower() in stem:
                hits.append((cat, w))
                break
    if hits:
        # 命中多个类目时，按表内顺序取先者
        order = {c: i for i, (c, _) in enumerate(FILENAME_TYPES)}
        hits.sort(key=lambda x: order[x[0]])
        return hits[0][0], hits
    if ext in EXT_TYPES:
        return EXT_TYPES[ext], [(EXT_TYPES[ext], ext)]
    return None, hits


n = hitn = okn = 0
bad = []
for r in rows:
    cat, hits = by_filename(r["name"], r["ext"])
    n += 1
    if not cat:
        continue
    hitn += 1
    if cat.split("/")[0] == r["dir"]:
        okn += 1
    else:
        bad.append((r["dir"], r["name"], cat, [h for _, h in hits]))

print(f"覆盖 {hitn}/{n} = {hitn/n:.0%}；命中中与目录一致 {okn}/{hitn} = {okn/hitn:.0%}")
print(f"全量口径 {okn}/{n} = {okn/n:.0%}")
print("不一致明细：")
for d, nm, c, h in bad:
    print(f"  [{d}] {nm[:40]} -> {c}  (命中 {h})")
