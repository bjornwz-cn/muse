#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""common.dup_delete —— 精确重复（dup:sha256）自动删除（2026-11-27 提速改动 1）。

背景：watcher 每轮重扫重分类，同一批约 5,573 个文件被重复建计划 7 次；
计划 263 里 1,668 项是 dup:sha256（其中 541 项置信度 ≥0.85），全被强制进
复核、自动归位几乎停摆（UTC 11-27 全天 operations 仅 14 条）。

本模块只删同时满足三条的收件箱副本（用户 2026-11-21 定下的删重复标准：
哈希相同＋保留份完整可用；an9 批次删 14 个重复源是先例）：
  (a) 保留份（evidence 路径）此刻仍存在、且在库内（工作/家庭/媒体库），
      不在收件箱内——两个都在收件箱时不自动删，防链式误删；
  (b) 保留份的 sha256 与副本一致（先比大小、再算 SHA）；
  (c) 保留份能打开/解码（照片 PIL 解码、视频 cv2 单帧、其他类型至少可读）。

删除纪律（硬性）：
  1. 先把逐名清单（含 src、保留份路径、sha256、大小）存盘再动手；
  2. 批量删除前用 sqlite3 backup API 备份 DB；
  3. 每条删除记 operations 表（dst_path="<deleted>" 哨兵、decided_by 注明），
     并把对应复核行判为 approved（同一 decided_by）；
  4. 绝不碰保留份；删前重算副本 SHA，文件被改过就跳过。

evidence 解析（resolve_kept_path）：watcher 的 dup 判定目前只做本轮内去重
（_dispatch_plan 不传 media Index / db_conn），evidence 几乎总是同批先出现
的收件箱路径；同批先出现的副本在本轮 apply 里先归位后，evidence 旧址已空。
此时查 operations 表找它最近一次被移走的目的地（只跟未回滚记录、最多 3 跳、
防环），再按同一三条标准验证目的地。解析不到、或任一验证不过 → 留复核。
"""
from __future__ import annotations

import json
import os
import sqlite3

from .db import (
    add_operation,
    decide_review,
    get_operation_for_src,
    get_review_for_src,
)
from .hashing import sha256_file
from .routing import library_top

# operations.dst_path 哨兵：此行是删除，不是移动；rollback_from_operations 跳过。
DELETED_DST = "<deleted>"
# operations.decided_by / review_queue.decided_by 的统一口径。
DECIDED_BY = "auto:dup-sha256"
# evidence 经 operations 表解析时最多跟几跳（防环）。
MAX_EVIDENCE_HOPS = 3

# 媒体扩展名兜底（media_core 不可用时）：仅用于判定“视频走 cv2 单帧验证”，
# 不是分类口径。正常情况下以 media_core.curate.VID_EXT 为准。
_VIDEO_EXTS_FALLBACK = frozenset({
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".flv", ".3gp", ".mpg",
    ".mpeg", ".mts", ".m2ts", ".webm", ".insv", ".vob", ".rmvb",
})


def _media_sets():
    """(image_exts, video_exts, undecodable_exts)，media_core 不可用时降级。"""
    try:
        from common.image_ext import get_image_exts
        img = get_image_exts()
    except Exception:  # noqa: BLE001
        img = frozenset({".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
                         ".tif", ".tiff", ".heic", ".heif"})
    try:
        from media_core.curate import UNDECODABLE_VIDEO_EXTS, VID_EXT
    except Exception:  # noqa: BLE001
        VID_EXT = _VIDEO_EXTS_FALLBACK
        UNDECODABLE_VIDEO_EXTS = frozenset({".rm", ".rmvb", ".ram"})
    return img, set(VID_EXT), set(UNDECODABLE_VIDEO_EXTS)


def _kept_readable(kept_path, ext):
    """保留份可读性验证（改动 1 的第 (c) 条）。返回 (True, "ok") 或 (False, 原因码)。

    照片：PIL 打开并 load；视频：先过 RealVideo 黑名单（改动 3），再 cv2
    读一帧；其他类型：打开并读 1 字节。任一依赖缺失时按失败处理（安全方向）。
    """
    ext = (ext or os.path.splitext(kept_path)[1]).lower()
    img_exts, vid_exts, undecodable = _media_sets()
    if ext in undecodable:
        return False, "kept-undecodable"
    if ext in img_exts:
        try:
            from PIL import Image
            with Image.open(kept_path) as im:
                im.load()
        except Exception:  # noqa: BLE001 - 打不开/解不开都算验证失败
            return False, "kept-undecodable"
        return True, "ok"
    if ext in vid_exts:
        try:
            import cv2
        except ImportError:
            return False, "kept-undecodable"
        cap = None
        try:
            cap = cv2.VideoCapture(kept_path)
            ok, _frame = cap.read()
        except Exception:  # noqa: BLE001
            return False, "kept-undecodable"
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:  # noqa: BLE001
                    pass
        return (True, "ok") if ok else (False, "kept-undecodable")
    try:
        with open(kept_path, "rb") as f:
            f.read(1)
    except OSError:
        return False, "kept-unreadable"
    return True, "ok"


def verify_kept_copy(src, kept_path, sha256, size, roots, *, ext=None):
    """三重验证保留份。返回 (True, "ok") 或 (False, 原因码)。

    src：待删副本路径；kept_path：保留份路径（已解析）；sha256/size：副本的
    指纹（plan-item 里的值）；roots：libraries.json 的 {top_id: root}；
    ext：副本扩展名（小写带点；不给则从 kept_path 取）。
    任一验证不过都返回 False，调用方留复核。
    """
    if not src or not kept_path:
        return False, "no-path"
    if not roots:
        return False, "no-roots"
    # (a1) 副本本身必须在收件箱里——库内文件绝不当作“待删副本”。
    if library_top(src, roots) != "inbox":
        return False, "src-not-in-inbox"
    if os.path.abspath(src) == os.path.abspath(kept_path):
        return False, "kept-same-as-src"
    if not os.path.isfile(kept_path):
        return False, "kept-missing"
    top = library_top(kept_path, roots)
    if top not in ("work", "family", "media"):
        if top == "inbox":
            return False, "kept-in-inbox"
        return False, "kept-not-in-library"
    # (b) 大小先比对（便宜），再算 SHA（贵）；任一不符都不删。
    if size is not None:
        try:
            if os.path.getsize(kept_path) != size:
                return False, "size-mismatch"
        except OSError:
            return False, "kept-missing"
    if not sha256:
        return False, "no-sha"
    if sha256_file(kept_path) != sha256:
        return False, "sha-mismatch"
    # (c) 保留份能打开/解码。
    return _kept_readable(kept_path, ext)


def resolve_kept_path(conn, evidence_path, *, max_hops=MAX_EVIDENCE_HOPS):
    """解析保留份路径：先按 evidence 原路径；若已不存在，查 operations 表
    找它最近一次被移走的目的地（同批先出现的副本在本轮 apply 里先归位，
    evidence 指向的收件箱旧址已空，目的地才是真正的保留份）。

    只跟未回滚的移动记录，最多 max_hops 跳、防环；跟到没有下一跳的终点，
    终点存在才是当前保留份。任一步失败返回 None（调用方留复核）。
    """
    if not evidence_path:
        return None
    cur = os.path.abspath(evidence_path)
    if os.path.isfile(cur):
        return cur
    seen = {cur}
    for _ in range(max_hops):
        row = get_operation_for_src(conn, cur)
        if row is None:
            return None
        dst = row.get("dst_path") or ""
        if not dst or dst == DELETED_DST:
            return None
        dst_abs = os.path.abspath(dst)
        if dst_abs in seen:
            return None
        seen.add(dst_abs)
        cur = dst_abs
        if get_operation_for_src(conn, cur) is None:
            # 终点：没有更新的移动记录，存在即为当前保留份。
            # （不在中间路径停下：同一路径可能后来被其他文件复用，
            # 真正的保留份在链的末端；SHA 验证还会再兜底一次。）
            return cur if os.path.isfile(cur) else None
    return None


def delete_exact_dup_copies(items, conn, plan_id, roots, *, manifest_dir,
                            backup_dir, dry_run=False):
    """批量删除精确重复副本。返回统计 dict。

    只处理 action=review、reason=dup:sha256、review_kind=dup 的项；每项先
    解析保留份、再三重验证，通过的才进删除清单。纪律：先写逐名清单（manifest
    JSONL，每行含 src/kept/evidence/sha256/size）→ 再备份 DB（sqlite3 backup
    API）→ 再逐条删；每条删前重算副本 SHA，删后记 operations 行（dst="<deleted>"、
    decided_by="auto:dup-sha256"）并把对应复核行判为 approved（同一 decided_by），
    plan_items 行置 done。dry_run 只验证、不写清单/备份、不删。
    """
    import datetime
    cands = [it for it in (items or [])
             if it.get("action") == "review"
             and str(it.get("reason") or "").startswith("dup:sha256")
             and it.get("review_kind") == "dup"]
    stats = {"candidates": len(cands), "deleted": 0, "would_delete": 0,
             "skipped": {}, "manifest": "", "backup": ""}
    verified = []
    for it in cands:
        src = it.get("src") or ""
        evidence = it.get("evidence") or ""
        sha = it.get("sha256") or ""
        size = it.get("size")
        kept = resolve_kept_path(conn, evidence)
        if kept is None:
            stats["skipped"]["kept-missing"] = \
                stats["skipped"].get("kept-missing", 0) + 1
            continue
        ok, why = verify_kept_copy(src, kept, sha, size, roots,
                                   ext=it.get("ext"))
        if not ok:
            stats["skipped"][why] = stats["skipped"].get(why, 0) + 1
            continue
        verified.append({"src": src, "kept": kept, "evidence": evidence,
                         "sha256": sha, "size": size})
    if not verified:
        return stats
    if dry_run:
        stats["would_delete"] = len(verified)
        return stats
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    os.makedirs(manifest_dir, exist_ok=True)
    os.makedirs(backup_dir, exist_ok=True)
    manifest_path = os.path.join(
        manifest_dir, "dup-delete-%s-plan%d.jsonl" % (ts, plan_id or 0))
    with open(manifest_path, "w", encoding="utf-8") as f:
        for v in verified:
            f.write(json.dumps({**v, "plan_id": plan_id, "verified_at": ts},
                               ensure_ascii=False) + "\n")
    stats["manifest"] = manifest_path
    backup_path = os.path.join(backup_dir, "nas-curator.db.bak-%s-predup" % ts)
    bkp = sqlite3.connect(backup_path)
    try:
        conn.backup(bkp)
    finally:
        bkp.close()
    stats["backup"] = backup_path
    for v in verified:
        src, sha = v["src"], v["sha256"]
        # 删前重算副本 SHA：plan 与 apply 之间文件被改过/换过就跳过。
        if not sha or sha256_file(src) != sha:
            stats["skipped"]["src-changed"] = \
                stats["skipped"].get("src-changed", 0) + 1
            continue
        try:
            os.unlink(src)
        except OSError:
            stats["skipped"]["unlink-failed"] = \
                stats["skipped"].get("unlink-failed", 0) + 1
            continue
        add_operation(conn, plan_id, src, DELETED_DST, sha, decided_by=DECIDED_BY)
        row = get_review_for_src(conn, src)
        if row is not None:
            decide_review(conn, row["id"], "approved", decided_by=DECIDED_BY)
        try:
            conn.execute("UPDATE plan_items SET status='done'"
                         " WHERE plan_id=? AND src_path=?", (plan_id, src))
            conn.commit()
        except Exception:  # noqa: BLE001 - 审计行更新失败不影响已删除的事实
            pass
        stats["deleted"] += 1
    return stats
