#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""media_core.engine —— 媒体 plan 层：把文件列表变成统一 plan-item。

只做规划（plan），不移动、不落盘。移动由统一 CLI 的 apply 流程执行
（走文档引擎的 apply_plan；本包不提供 apply_plan）。

判定顺序（与原 curate.py main() 一致，未改逻辑、未改阈值）：
  解码失败 → 待确认；SHA256 命中索引 → 疑似重复；
  dHash 近重复（汉明距离 ≤ phash_max_distance）→ 疑似重复；
  模糊（blur < blur_min，仅照片）→ 模糊；
  SigLIP + ChineseCLIP 分类，白底规则优先；
  margin < 0.12 或 top1 < 0.35 → 待确认；否则接受。

视频 v1：抽帧 = 首帧 + 每 10 秒一帧，上限 6 帧（见 curate.video_frames），
每帧走同一套图片分类后取众数，票数相同取首帧结果；抽帧失败进 review
（decode_failed）。

import 顺序（cv2 先于 torch）：本模块本身绝不 import torch；唯一可能触发
torch 加载的路径是 :class:`~media_core.curate.Classifier` 的实例化，
该路径在 curate.py 内已由 ``ensure_cv2_first()`` 显式守住（先 import cv2
加载 opencv .libs，再 import torch，见 curate 模块 docstring）。
本模块提供 :func:`build_classifier` 作为唯一推荐的构造入口（内部先调
ensure_cv2_first 再实例化）；``plan_media_files`` 只接受已实例化的
classifier，不在内部构造、不触发任何模型加载。

统一 plan-item dict 格式（curator_core 与 CLI 按此对接）：
  {"src": str, "dst": str, "new_name": str, "category": str,
   "confidence": float, "reason": str, "evidence": str,
   "action": "move"|"rename"|"tag"|"skip"|"review",
   "kind": "media", "size": int, "ext": str, "sha256": str,
   "tags": list[str], "review_kind": str|None, "file_id": int|None}
  * kind 恒为 "media"；action="review" 时 dst=""，review_kind 取值
    low_conf|dup|blur|ambiguous（shell 预留给 iCloud 占位符，本层不产生）。
  * confidence：接受的分类用 top1；待确认用 top1（封顶 0.45）；模糊用 0.3；
    重复用 0.9（SHA256 精确）或 0.8（dHash 近重复）。
  * tags 由 common.tags.gen_media_tags 生成（取其返回的标签名列表）。
"""
from __future__ import annotations

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from .curate import (  # noqa: E402
    IMG_EXT,
    MEDIA_EXTS,
    Classifier,
    decide,
    decode_item_frames,
    dhash_int,
    ensure_cv2_first,
    ensure_readable,
    get_image_exts,
    hamming,
    media_exts,
    scan_inbox,
    sha256_of,
    stable_filter,
    target_dir,
    _image_features,
)

try:
    from common.tags import gen_media_tags as _gen_media_tags
except Exception:  # common 层不可用时降级为本地同义实现
    _gen_media_tags = None

__all__ = [
    "plan_media_files",
    "plan_inbox",
    "build_classifier",
    "MEDIA_EXTS",
    "REVIEW_KINDS",
]

REVIEW_KINDS = ("low_conf", "dup", "blur", "ambiguous", "shell")

# 默认阈值（与只读源 config.json 一致；cfg 里缺失时兜底，不改判定口径）。
_DEFAULT_THRESHOLDS = {
    "margin_min": 0.12,
    "score_min": 0.35,
    "blur_min": 50,
    "phash_max_distance": 5,
    "near_dup_enabled": True,
}
_DEFAULT_REVIEW_DIRS = {
    "待确认": "_Review/待确认",
    "模糊": "_Review/模糊",
    "疑似重复": "_Review/疑似重复",
}


def build_classifier(cfg):
    """构造 Classifier 的推荐入口：先 ensure_cv2_first() 再实例化。

    显式守住 cv2-before-torch 顺序（原因见 curate.ensure_cv2_first）。
    plan_media_files 本身不构造 classifier；调用方用本函数建好再传入。
    """
    ensure_cv2_first()
    return Classifier(cfg)


def _effective_cfg(cfg):
    """补齐 thresholds/review_dirs/photo_dirs/video_dirs 缺省值（不改阈值）。"""
    eff = dict(cfg or {})
    th = dict(_DEFAULT_THRESHOLDS)
    th.update((cfg or {}).get("thresholds") or {})
    eff["thresholds"] = th
    eff.setdefault("review_dirs", dict(_DEFAULT_REVIEW_DIRS))
    eff.setdefault("photo_dirs", {})
    eff.setdefault("video_dirs", {})
    return eff


def _media_tag_names(category, kind_cn):
    """common.tags.gen_media_tags 生成的标签名列表（list[str]）。

    只在接受的分类上取场景标签；review/skip 项传空 category，只剩媒介
    类型标签（照片/视频）。common 层不可用时用同义本地逻辑兜底。
    """
    if _gen_media_tags is not None:
        try:
            return [t[0] for t in _gen_media_tags(category or "", kind=kind_cn)]
        except Exception:
            pass
    names = []
    if category:
        leaf = category.replace("\\", "/").rstrip("/").split("/")[-1]
        if leaf and leaf not in ("照片", "视频", "待确认", "模糊"):
            names.append(leaf)
    if kind_cn in ("照片", "视频"):
        names.append(kind_cn)
    return names


def _base_item(src, name, kind_cn, size, ext, sha256=""):
    """plan-item 骨架（kind 恒为 media，file_id 在 v1 为 None）。"""
    return {
        "src": src,
        "dst": "",
        "new_name": name,
        "category": "",
        "confidence": 0.0,
        "reason": "",
        "evidence": "",
        "action": "review",
        "kind": "media",
        "size": int(size),
        "ext": ext,
        "sha256": sha256,
        "tags": [],
        "review_kind": None,
        "file_id": None,
        # P2(P1.1)：与文档 plan-item 同形；媒体无规则分，score 恒 0；无 unit，恒不豁免。
        "score": 0,
        "auto_exempt": False,
    }


def _review_item(base, category, confidence, reason, evidence, review_kind, kind_cn):
    base.update({
        "dst": "",
        "category": category,
        "confidence": float(confidence),
        "reason": reason,
        "evidence": evidence,
        "action": "review",
        "tags": _media_tag_names("", kind_cn),
        "review_kind": review_kind,
    })
    return base


def _classify_video_frames(classifier, frames, row, cfg):
    """视频多帧分类：每帧走同一套图片分类（scores + decide），取众数投票。

    每帧先算自身 blur/white/colors/ar 再调 decide（与照片同一套判定）。
    只统计 why == 'accepted' 的帧；票数最多者胜，票数相同取首帧结果；
    无一帧 accepted 时返回首帧的判定（一般为 low_confidence）。
    返回 (cat, cat2, s1, s2, why)，口径与 decide() 一致。
    """
    results = []
    for img in frames:
        try:
            blur, white, colors, ar = _image_features(img)
        except Exception as e:
            results.append(("待确认", "待确认", 0.0, 0.0, "decode_failed"))
            continue
        try:
            sc = classifier.scores(img, "视频")
        except Exception:
            results.append(("待确认", "待确认", 0.0, 0.0, "decode_failed"))
            continue
        results.append(decide(row, sc, blur, white, colors, ar, "", cfg))
    if not results:
        return "待确认", "待确认", 0.0, 0.0, "decode_failed"
    accepted = [(i, r) for i, r in enumerate(results) if r[4] == "accepted"]
    if not accepted:
        return results[0]
    votes = {}
    for i, r in accepted:
        votes.setdefault(r[0], []).append((i, r))
    # 票数最多者胜；票数相同取首帧（最早出现帧）的结果。
    winner = max(votes.items(), key=lambda kv: (len(kv[1]), -kv[1][0][0]))
    first_idx, first_res = winner[1][0]
    return first_res


def plan_media_files(paths, cfg, *, classifier=None, index=None, library_root=None):
    """规划一批媒体文件：返回 (items, stats)。

    paths：待处理文件绝对路径列表；cfg：config.json 同结构 dict；
    classifier：已实例化的 Classifier 或 None（None 时只做去重/解码/
    模糊检测，不做内容分类，分类字段留空进 review）；
    index：已加载的 Index 或 None；library_root：媒体库根目录，
    缺省取 cfg['library_root']。

    媒体绝不碰图书/音乐：入口处先断言扩展名——不在照片/视频集合里的
    一律 action="skip"、reason="not-media"（含 .pdf/.mp3/.lrv 等）。

    本轮内去重：除索引比对外，同一批里先出现的相同 SHA256 / 相近 dHash
    也会让后一份进 review(dup)——与库内去重同一口径，证据为先出现的文件。
    """
    eff = _effective_cfg(cfg)
    th = eff["thresholds"]
    lib_root = library_root or eff.get("library_root") or ""

    items = []
    stats = {"scanned": 0, "skipped": 0, "media": 0, "dup": 0, "blurry": 0,
             "low_conf": 0, "decode_failed": 0, "no_classifier": 0,
             "review": 0, "accepted": 0}
    seen_sha = {}    # sha256 -> 先出现的文件路径（本轮内去重）
    seen_dhash = []  # [(dhash, path)]（本轮内近重复）

    for path in paths:
        stats["scanned"] += 1
        name = os.path.basename(path)
        ext = os.path.splitext(name)[1].lower()
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0

        # P2(P1.1)：.mts 碰撞——node_modules 下的 .d.mts 是 TypeScript 声明文件，
        # 不是 AVCHD 视频；路径含 node_modules 或文件名为 .d.mts 一律按 not-media 跳过。
        if ext == ".mts" and (name.lower().endswith(".d.mts")
                             or (os.sep + "node_modules" + os.sep) in path):
            item = _base_item(path, name, "", size, ext, sha256="")
            item.update({"action": "skip", "reason": "not-media",
                         "evidence": "typescript-decl", "tags": [], "review_kind": None})
            items.append(item)
            stats["skipped"] += 1
            continue

        # 入口断言：非媒体扩展名一律 skip（图书/音乐绝不碰）。
        # 0.1.5：用函数 media_exts()，随图片扩展名配置动态变化。
        if ext not in media_exts():
            item = _base_item(path, name, "", size, ext, sha256="")
            item.update({"action": "skip", "reason": "not-media",
                         "evidence": "", "tags": [], "review_kind": None})
            items.append(item)
            stats["skipped"] += 1
            continue
        stats["media"] += 1
        kind_cn = "照片" if ext in get_image_exts() else "视频"
        row = {"path": path, "name": name, "kind": kind_cn,
               "size": size, "mtime": 0, "ext": ext}
        try:
            row["mtime"] = os.path.getmtime(path)
        except OSError:
            pass

        # 1) SHA256 精确去重（先查库内索引，再查本轮已见）。
        sha = sha256_of(path)
        if sha.startswith("ERR"):
            sha = ""
        dup_path = None
        if sha and index is not None:
            dup_path = index.dup_of(sha)
        if dup_path is None and sha:
            dup_path = seen_sha.get(sha)
        if dup_path:
            item = _review_item(_base_item(path, name, kind_cn, size, ext, sha),
                                "疑似重复", 0.9, "dup:sha256", dup_path, "dup", kind_cn)
            items.append(item)
            stats["dup"] += 1
            stats["review"] += 1
            continue
        if sha:
            seen_sha[sha] = path

        # 2) dHash 近重复（仅照片；thresholds.near_dup_enabled 开关与原一致）。
        dh = None
        if kind_cn == "照片" and th.get("near_dup_enabled"):
            try:
                from PIL import Image
                src, tmpc = ensure_readable(path)
                if src:
                    try:
                        dh = dhash_int(Image.open(src).convert("RGB"))
                    finally:
                        if tmpc and os.path.exists(tmpc):
                            os.unlink(tmpc)
            except Exception:
                dh = None
        if dh is not None:
            near = None
            if index is not None and index.dhash_list:
                near = index.near_dup_of(dh, int(th["phash_max_distance"]))
            if near is None:
                for v, p in seen_dhash:
                    d = hamming(dh, v)
                    if d <= int(th["phash_max_distance"]) and (near is None or d < near[0]):
                        near = (d, p)
            if near:
                item = _review_item(_base_item(path, name, kind_cn, size, ext, sha),
                                    "疑似重复", 0.8, "dup:dhash:%d" % near[0],
                                    near[1], "dup", kind_cn)
                items.append(item)
                stats["dup"] += 1
                stats["review"] += 1
                continue
            seen_dhash.append((dh, path))

        # 3) 解码（视频：多帧采样；失败 → review/decode_failed）。
        frames, blur, white, colors, ar, note = decode_item_frames(row)
        if note or not frames:
            item = _review_item(_base_item(path, name, kind_cn, size, ext, sha),
                                "待确认", 0.0, "decode_failed:%s" % (note or "noframe"),
                                "", "ambiguous", kind_cn)
            items.append(item)
            stats["decode_failed"] += 1
            stats["review"] += 1
            continue

        # 4) 模糊（仅照片；与 decide() 的 blur 门一致，但先于模型推理判定，
        #    结果相同、省一次推理；classifier=None 时同样生效）。
        if kind_cn == "照片" and blur < th["blur_min"]:
            item = _review_item(_base_item(path, name, kind_cn, size, ext, sha),
                                "模糊", 0.3, "blurry:%.1f" % blur,
                                "", "blur", kind_cn)
            items.append(item)
            stats["blurry"] += 1
            stats["review"] += 1
            continue

        # 5) 内容分类（classifier=None 时分类字段留空进 review）。
        if classifier is None:
            item = _review_item(_base_item(path, name, kind_cn, size, ext, sha),
                                "", 0.0, "no_classifier", "", "ambiguous", kind_cn)
            items.append(item)
            stats["no_classifier"] += 1
            stats["review"] += 1
            continue

        if kind_cn == "视频":
            cat, cat2, s1, s2, why = _classify_video_frames(classifier, frames, row, eff)
        else:
            sc = classifier.scores(frames[0], "照片")
            cat, cat2, s1, s2, why = decide(row, sc, blur, white, colors, ar, "", eff)
        if why == "accepted":
            # decide 接受分支：cat=top1、cat2=top2。
            evidence = "%s %.2f / %s %.2f" % (cat, s1, cat2, s2)
        else:
            # 低置信分支：decide 把 top1 类名放在 cat2 位置（cat='待确认'），
            # top2 类名未返回，evidence 只写 top1。
            evidence = "%s %.2f" % (cat2, s1) if cat2 != "待确认" else ""

        if why == "accepted":
            rel = target_dir(eff, kind_cn, cat)
            item = _base_item(path, name, kind_cn, size, ext, sha)
            item.update({
                "dst": os.path.join(lib_root, rel, name),
                "category": cat,
                "confidence": float(s1),
                "reason": "siglip:%.2f" % s1,
                "evidence": evidence,
                "action": "move",
                "tags": _media_tag_names(cat, kind_cn),
                "review_kind": None,
            })
            items.append(item)
            stats["accepted"] += 1
            continue

        # 低置信 → review（decide 已返回 cat='待确认'）。
        margin = s1 - s2
        if margin < th["margin_min"]:
            reason = "low:margin:%.2f" % margin
        else:
            reason = "low:score:%.2f" % s1
        item = _review_item(_base_item(path, name, kind_cn, size, ext, sha),
                            "待确认", min(float(s1), 0.45), reason, evidence,
                            "low_conf", kind_cn)
        items.append(item)
        stats["low_conf"] += 1
        stats["review"] += 1

    return items, stats


def plan_inbox(cfg, *, classifier=None, index=None):
    """扫 cfg['inbox']，stable_filter（settle_minutes）后调 plan_media_files。

    单轮上限取 cfg['max_files_per_run']（默认 300，与原 main 一致）。
    state 不做跨轮持久化（原 main 的 state.json 读写由调用方/编排器负责）。
    返回 (items, stats)；stats 在 plan_media_files 的 stats 基础上加
    waiting（本轮等待稳定的文件数）与 scan_skipped（扫描跳过统计）。
    """
    rows, skipped = scan_inbox(cfg)
    ready, waiting = stable_filter(rows, cfg, {})
    limit = cfg.get("max_files_per_run", 300) or 300
    batch = ready[:limit]
    items, stats = plan_media_files(
        [r["path"] for r in batch], cfg,
        classifier=classifier, index=index,
        library_root=cfg.get("library_root"))
    stats["waiting"] = len(waiting)
    stats["scan_skipped"] = skipped
    return items, stats
