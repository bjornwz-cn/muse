#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""common.routing —— 家庭 vs 工作分流（P2，P1.1 第 6 项）。

输入源（统一收件箱 / 笔记本备份）的文档默认归工作库；本模块按启发式判断
是否应归家庭库。家庭库第一级是人（刘音 / 王子婴 / 王子轩 / 王现琦 / 王震 / 韩红），
第二级才是事项/类型。

判定口径（启发式，P2 先跑通，精细规则以后调）：
  1. 文件名/正文/标签含家庭成员名（除王震外）→ 家庭库 / 该人名；
  2. 王震的名字同时出现在工作文件里，单独出现不算家庭信号——须同时命中
     家庭关键词才归家庭库 / 王震；
  3. 只命中家庭关键词、无人名 → 家庭库 / 待分人；
  4. 以上都不命中 → 工作库。

家庭关键词覆盖：身份户籍、房产物业、教育成长、医疗健康、婚育继承、家庭财务。
本模块是纯函数，不读盘、不调模型；规则数据另见 curator_core/rules.family.json。
"""
from __future__ import annotations

FAMILY_PERSONS = ("刘音", "王子婴", "王子轩", "王现琦", "王震", "韩红")

# 王震以外的家庭成员：名字单独出现即家庭信号（极少出现在工作文件里）。
OTHER_PERSONS = tuple(p for p in FAMILY_PERSONS if p != "王震")

FAMILY_KEYWORDS = (
    # 身份户籍
    "户口", "户籍", "身份证", "护照", "出生", "出生证", "结婚", "离婚", "遗嘱", "继承",
    "族谱", "家谱",
    # 房产物业
    "房产", "房产证", "不动产", "物业", "水电", "燃气", "房贷", "车位", "装修",
    # 教育成长
    "幼儿园", "小学", "中学", "学校", "学费", "奶粉", "育儿", "疫苗", "接种",
    "产检", "孕检", "出生证明",
    # 医疗健康
    "体检", "医保", "就诊", "病历", "处方",
    # 家庭财务
    "社保", "公积金", "年夜饭", "红包", "压岁钱",
)

UNASSIGNED_PERSON = "待分人"


def route_document(name="", text="", tags=()):
    """返回 (library, person)：library 为 "family" 或 "work"。

    name：文件名；text：正文/证据片段；tags：标签名列表。
    """
    hay = " ".join([name or "", text or "", " ".join(tags or [])])
    for person in OTHER_PERSONS:
        if person and person in hay:
            return "family", person
    has_wangzhen = "王震" in hay
    has_kw = any(kw in hay for kw in FAMILY_KEYWORDS)
    if has_wangzhen and has_kw:
        return "family", "王震"
    if has_kw:
        return "family", UNASSIGNED_PERSON
    return "work", None


def rewrite_family_dst(dst, family_root, person, target_name=""):
    """把一条已算好的工作库 dst 改写到家庭库：<family_root>/<person>/<原相对路径>。

    dst 为工作库下的绝对路径（或相对路径）；返回改写后的绝对路径。
    原相对路径的第一段若是类目（如 01-合同与协议），保留为第二级。
    """
    import os
    if not dst:
        return dst
    # dst 形如 <work_root>/<category>/<target> 或 <work_root>/<category>
    parts = [p for p in dst.split(os.sep) if p]
    # 去掉工作库根：相对路径取最后两段（类目 + 文件名）或最后一段
    if target_name:
        rel = parts[-2:] if len(parts) >= 2 else [target_name]
    else:
        rel = parts[-1:]
    return os.path.join(family_root, person, *rel)
