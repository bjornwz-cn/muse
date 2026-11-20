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

# ---------------------------------------------------------------- 0.1.2：路径强信号 / 人优先 / holding 区
# 顶层口径（2026-11-20 王震定）：「工作」指王震本人的工作文件；
# 其他家庭成员的工作文件一律归其人名下（家庭/<成员>/工作/<内容子目录>）。
import os as _os
import re as _re

TOP_LABELS = {"work": "工作", "family": "家庭", "media": "媒体",
              "inbox": "收件箱", "mac-backup": "笔记本备份"}
LABEL_TO_TOP = {v: k for k, v in TOP_LABELS.items()}

DUP_HOLDING_TOP = "99-待确认"
DUP_HOLDING_SUB = "06-重复待确认"

# 工作类目 -> 家庭成员工作内容子目录（用户口径：会议、票据等）。
WORK_CONTENT_SUBDIR = {
    "09-会议与汇报": "会议",
    "07-财务与经营分析": "票据",
    "03-合同与协议": "合同",
    "01-公司资质与证照": "证照",
    "02-产品与注册": "产品",
    "10-周报与工作总结": "周报",
    "12-招投标": "招投标",
    "05-销售与业务数据": "业务",
    "08-运营与项目": "项目",
    "04-供应商与采购": "采购",
    "06-库存与物流": "物流",
    "11-人事与行政": "行政",
    "13-图片与影像": "影像",
    "14-工具与其他": "其他",
    "06-其他": "其他",
}


def roots_from_config(cfg):
    """libraries.json -> {top_id: root}（含收件箱与笔记本备份输入源）。"""
    roots = {}
    for lib in (cfg or {}).get("libraries") or []:
        if lib.get("id") and lib.get("root"):
            roots[lib["id"]] = lib["root"]
    inbox = (cfg or {}).get("inbox") or {}
    if inbox.get("path"):
        roots.setdefault("inbox", inbox["path"])
    for src in (cfg or {}).get("input_sources") or []:
        if src.get("id") and src.get("root"):
            roots.setdefault(src["id"], src["root"])
    return roots


def library_top(path, roots):
    """路径所属顶层：work/family/media/inbox/mac-backup；都不在返回 None。"""
    if not path or not roots:
        return None
    p = _os.path.abspath(path)
    best, best_len = None, -1
    for top, root in roots.items():
        if not root:
            continue
        r = _os.path.abspath(root)
        if (p == r or p.startswith(r.rstrip(_os.sep) + _os.sep)) and len(r) > best_len:
            best, best_len = top, len(r)
    return best


def top_label(top):
    """顶层 id -> 中文名；未知返回 '其他'。"""
    return TOP_LABELS.get(top or "", "其他")


# 0.1.6：复核队列文件夹分组——通用容器目录名（下载/桌面/文稿等）本身不是
# 有意义的文件夹，下钻一级取真正的项目/内容文件夹（如 mac-backup 下的
# 下载/PaddleOCR-3.7.0，而不是把全部下载混在一张「下载」卡片里）。
GENERIC_FOLDER_CONTAINERS = ("下载", "Download", "桌面", "Desktop",
                             "文稿", "Documents")


def review_folder_for(src, roots):
    """复核项所属文件夹（供队列按文件夹分组）：返回
    (folder_key, top, rel_folder, display)。

    folder_key = "<top>:<rel_folder>"；rel_folder 为库根下的相对文件夹，
    取第一级目录；第一级为通用容器名时下钻一级（至多三级），如
    mac-backup 的「下载/PaddleOCR-3.7.0」、家庭库的「刘音」、
    工作库的「归档」。src 不在任何库根下时 top 为 None，folder_key
    为 "other:"；文件直接在库根下时 rel_folder 为 ""。
    """
    top = library_top(src, roots) if src else None
    if not top or top not in (roots or {}):
        return ("other:", None, "", "其他")
    root = roots[top]
    try:
        rel = _os.path.relpath(_os.path.dirname(_os.path.abspath(src)),
                               _os.path.abspath(root))
    except ValueError:
        return ("other:", None, "", "其他")
    if rel in (".", ""):
        return (f"{top}:", top, "", top_label(top))
    parts = [p for p in rel.split(_os.sep) if p not in ("", ".")]
    if not parts:
        return (f"{top}:", top, "", top_label(top))
    depth = 1
    while (depth < len(parts) and parts[depth - 1] in GENERIC_FOLDER_CONTAINERS
           and depth < 3):
        depth += 1
    folder = _os.sep.join(parts[:depth])
    return (f"{top}:{folder}", top, folder.replace(_os.sep, "/"),
            parts[depth - 1])


def family_person_of(src, family_root="/vol1/1000/家庭文件"):
    """家庭库路径里的人：<family_root>/<成员>/… -> 成员名；不在家庭库返回 ''。

    family_root 缺省为线上家庭库根；测试/自定义库根由调用方传入。
    """
    if not src or not family_root:
        return ""
    try:
        rel = _os.path.relpath(_os.path.abspath(src), _os.path.abspath(family_root))
    except ValueError:
        return ""
    if rel.startswith(".." + _os.sep) or rel in ("..", ".", ""):
        return ""
    return rel.split(_os.sep)[0]


def path_prior_label(src, roots):
    """路径先验标签：文件在库内的相对父目录，如 '家庭/王震/通话录音'。

    输入源（收件箱/笔记本备份）与库外路径返回 None——文件尚未归位，
    路径不能当先验。
    """
    top = library_top(src, roots)
    if top not in ("work", "family", "media"):
        return None
    root = roots[top]
    rel = _os.path.relpath(_os.path.dirname(_os.path.abspath(src)),
                           _os.path.abspath(root))
    if rel in (".", ""):
        return top_label(top)
    return top_label(top) + "/" + rel.replace(_os.sep, "/")


def is_work_category(category):
    """类目是否工作库类目（01-14 号段；含默认 06-其他）。家庭个人类目为 15-19。"""
    top = (category or "").split("/")[0]
    m = _re.match(r"^(\d+)-", top)
    if not m:
        return False
    try:
        return 1 <= int(m.group(1)) <= 14
    except ValueError:
        return False


def family_work_subdir(category):
    """工作类目 -> 家庭成员工作内容子目录（会议/票据/…；未列出的去编号取简称）。"""
    top = (category or "").split("/")[0]
    if top in WORK_CONTENT_SUBDIR:
        return WORK_CONTENT_SUBDIR[top]
    short = _re.sub(r"^\d+-", "", top).strip() or "其他"
    return short


def rewrite_family_work_dst(family_root, person, target_name, category):
    """家庭成员的工作内容：<family_root>/<person>/工作/<内容子目录>/<target>。"""
    return _os.path.join(family_root, person, "工作",
                         family_work_subdir(category), target_name)


def dup_holding_dst(src, roots):
    """重复项 holding 区（批准后移入，不删除）：按源文件所在顶层落到同库的
    99-待确认/06-重复待确认/。家庭按人分（保持人优先结构）；笔记本备份
    落在备份树内的同名 holding 目录；收件箱/未知来源落收件箱 holding。"""
    top = library_top(src, roots)
    name = _os.path.basename(src or "")
    if top == "family":
        person = family_person_of(src, (roots or {}).get("family")) or UNASSIGNED_PERSON
        return _os.path.join(roots["family"], person,
                             DUP_HOLDING_TOP, DUP_HOLDING_SUB, name)
    if top in ("work", "media"):
        return _os.path.join(roots[top], DUP_HOLDING_TOP, DUP_HOLDING_SUB, name)
    if top == "mac-backup":
        return _os.path.join(roots["mac-backup"],
                             DUP_HOLDING_TOP, DUP_HOLDING_SUB, name)
    base = (roots or {}).get("inbox") or (roots or {}).get("work") \
        or "/vol1/1000/收件箱"
    return _os.path.join(base, DUP_HOLDING_TOP, DUP_HOLDING_SUB, name)


def _conf_of(item):
    try:
        return float(item.get("confidence") or 0)
    except (TypeError, ValueError):
        return 0.0


def apply_path_prior_guard(items, roots, auto_threshold=0.85):
    """路径强信号后处理（0.1.2）：低置信建议不得跨顶层移动；只有高置信
    才能建议跨顶层。只改 items 里的建议（action/dst/category/reason），
    不动任何文件；unit 整目录项（auto_exempt）是结构性判定，不拦。

    规则：move/rename 项，conf < auto_threshold，且 dst 顶层 != src 顶层
    （src/dst 顶层都能认出时）-> 转 review（dst 清空，review_kind=low_conf，
    置信度压到 0.45，reason 追加 path-prior 标注）。
    返回 (items, {"guarded": n})。
    """
    guarded = 0
    for it in items or []:
        if it.get("action") not in ("move", "rename"):
            continue
        if it.get("auto_exempt"):
            continue
        src, dst = it.get("src") or "", it.get("dst") or ""
        if not src or not dst:
            continue
        src_top = library_top(src, roots)
        dst_top = library_top(dst, roots)
        if src_top is None or dst_top is None or src_top == dst_top:
            continue
        conf = _conf_of(it)
        if conf >= auto_threshold:
            continue
        it["action"] = "review"
        it["dst"] = ""
        it["review_kind"] = "low_conf"
        it["confidence"] = min(conf, 0.45)
        reason = it.get("reason") or ""
        it["reason"] = (reason + f";path-prior:低置信{conf:.2f}不跨顶层"
                        f"（{top_label(src_top)}→{top_label(dst_top)}）")
        guarded += 1
    return items, {"guarded": guarded}


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
