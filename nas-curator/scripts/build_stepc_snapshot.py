#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤 C 复检快照：从 review_queue 里取 batch4 之前就有的行（非 batch4），
src 在收件箱内、在盘、且 reason 不含“加密”的文件，写快照 TSV
（path, size, mtime, kind），供 year_siglip_stepc.py 与 year_doc_stepc.py 用。
只读 DB、不写库、不动文件。加密的 737 个（reason 含“加密”）排除在快照外。
kind 按扩展名：照片/视频/文档/其他（与第四批快照同口径）。
"""
import json
import os
import sqlite3
from collections import Counter

DB = "/data/nas-curator.db"
OUT = "/data/stepc_snapshot.tsv"

IMG_EXTS = {".jpg", ".jpeg", ".heic", ".png", ".heif", ".webp", ".gif", ".nef"}
VID_EXTS = {".mov", ".mp4", ".mts", ".vob", ".avi", ".wmv"}
DOC_EXTS = {".pdf", ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt",
            ".txt", ".csv", ".md", ".html", ".webloc", ".eml",
            ".zip", ".rar", ".eddx", ".emmx", ".osheet", ".odoc"}


def kind_of(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in IMG_EXTS:
        return "照片"
    if ext in VID_EXTS:
        return "视频"
    if ext in DOC_EXTS:
        return "文档"
    return "其他"


def valid_existing(payload):
    """已有 payload 的分类是否可复用：真实类目且 confidence>=0.35。"""
    cat = payload.get("category")
    try:
        conf = float(payload.get("confidence") or 0)
    except Exception:
        conf = 0.0
    if not cat or cat in ("待确认", "模糊", "疑似重复"):
        return False
    return conf >= 0.35


def main():
    conn = sqlite3.connect(DB, timeout=30)
    rows = conn.execute(
        "SELECT id, kind, payload FROM review_queue WHERE status='pending'"
    ).fetchall()
    conn.close()
    total = encrypted = gone = 0
    picked = []
    reusable = 0
    for rid, kind, payload in rows:
        d = json.loads(payload)
        src = d.get("src", "")
        if not src.startswith("/vol1/1000/收件箱/"):
            continue
        if d.get("batch") == "batch4":
            continue
        total += 1
        if "加密" in str(d.get("reason", "")):
            encrypted += 1
            continue
        try:
            st = os.stat(src)
        except OSError:
            gone += 1
            continue
        if valid_existing(d):
            reusable += 1
        picked.append((src, st.st_size, st.st_mtime, kind_of(src), rid))
    picked.sort()
    with open(OUT, "w", encoding="utf-8") as fh:
        for src, size, mtime, k, rid in picked:
            fh.write("%s\t%d\t%.6f\t%s\n" % (src, size, mtime, k))
    print("stepc candidates (non-batch4, inbox): %d" % total)
    print("  encrypted skipped (reason 含加密): %d" % encrypted)
    print("  gone (src 不在盘): %d" % gone)
    print("  snapshot written: %d -> %s" % (len(picked), OUT))
    print("  kind 分布: %s" % dict(Counter(k for *_, k, _ in picked)))
    print("  其中已有有效分类可复用（SigLIP 会跳过）: %d" % reusable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
