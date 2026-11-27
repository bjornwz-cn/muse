#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-11-27 提速改动 2 回归：watcher 跳过已在复核队列的文件。

覆盖：有复核行且 size/mtime 未变 → 本轮跳过；无行 / 指纹缺失（旧行）/
文件改动过 → 正常纳入；复核行 payload 带指纹、已有行刷新指纹；
文档引擎 plan_files 在正文抽取前过滤；媒体路径在 _dispatch_plan 里过滤；
已裁决的行同样跳过。
只用 tmp 目录与临时 DB，不碰 NAS、不碰生产库。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import curator as CUR  # noqa: E402
from common import db as DB  # noqa: E402
from curator_core.engine import plan_files  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES_PATH = os.path.join(PROJECT_ROOT, "curator_core", "rules.example.json")


def _load_rules():
    with open(RULES_PATH, encoding="utf-8") as f:
        return json.load(f)


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _fp(path):
    return os.path.getsize(path), os.path.getmtime(path)


def test_db_review_fingerprint_helpers(tmp_path):
    db = str(tmp_path / "t.db")
    conn = DB.connect(db)
    DB.init_db(db)
    src = "/tmp/x/b.jpg"
    assert DB.get_review_for_src(conn, src) is None
    DB.add_review(conn, plan_id=None, kind="low_conf",
                  payload={"src": src, "category": "c", "confidence": 0.4})
    row = DB.get_review_for_src(conn, src)
    assert row is not None and row["status"] == "pending"
    fps = DB.list_reviewed_fingerprints(conn)
    assert fps[src]["size"] is None and fps[src]["mtime"] is None
    # 指纹缺失的旧行：刷新写入
    assert DB.refresh_review_fingerprint(conn, src, 123, 456.5) is True
    fps = DB.list_reviewed_fingerprints(conn)
    assert fps[src]["size"] == 123 and fps[src]["mtime"] == 456.5
    # 指纹已一致：不再写
    assert DB.refresh_review_fingerprint(conn, src, 123, 456.5) is False
    # 无行：不写
    assert DB.refresh_review_fingerprint(conn, "/tmp/x/nope.jpg", 1, 1.0) is False
    conn.close()


def test_skipper_skips_unchanged(tmp_path):
    src_dir = tmp_path / "inbox"
    src_dir.mkdir()
    p = _write(str(src_dir / "a.txt"), "内容 A")
    size, mtime = _fp(p)
    db = str(tmp_path / "t.db")
    conn = DB.connect(db)
    DB.init_db(db)
    DB.add_review(conn, plan_id=None, kind="low_conf",
                  payload={"src": p, "size": size, "mtime": mtime})
    skipper = CUR._ReviewSkipper(conn)
    assert skipper.should_skip(p) is True
    assert skipper.skipped == 1
    conn.close()


def test_skipper_includes_changed_and_legacy(tmp_path):
    src_dir = tmp_path / "inbox"
    src_dir.mkdir()
    # 改动过：mtime 变了
    changed = _write(str(src_dir / "changed.txt"), "旧内容")
    size, mtime = _fp(changed)
    time.sleep(0.02)
    _write(changed, "新内容更长一些")
    # 指纹缺失的旧行
    legacy = _write(str(src_dir / "legacy.txt"), "旧行内容")
    # 无行
    fresh = _write(str(src_dir / "fresh.txt"), "新文件")
    db = str(tmp_path / "t.db")
    conn = DB.connect(db)
    DB.init_db(db)
    DB.add_review(conn, plan_id=None, kind="low_conf",
                  payload={"src": changed, "size": size, "mtime": mtime})
    DB.add_review(conn, plan_id=None, kind="low_conf",
                  payload={"src": legacy})
    skipper = CUR._ReviewSkipper(conn)
    assert skipper.should_skip(changed) is False
    assert skipper.should_skip(legacy) is False
    assert skipper.should_skip(fresh) is False
    assert skipper.skipped == 0
    conn.close()


def test_skipper_skips_decided_rows(tmp_path):
    """已裁决的行同样跳过（has_review_for_src 口径：pending/已裁决都算已有）。"""
    src_dir = tmp_path / "inbox"
    src_dir.mkdir()
    p = _write(str(src_dir / "a.txt"), "内容")
    size, mtime = _fp(p)
    db = str(tmp_path / "t.db")
    conn = DB.connect(db)
    DB.init_db(db)
    rid = DB.add_review(conn, plan_id=None, kind="low_conf",
                        payload={"src": p, "size": size, "mtime": mtime})
    DB.decide_review(conn, rid, "approved")
    skipper = CUR._ReviewSkipper(conn)
    assert skipper.should_skip(p) is True
    conn.close()


def _review_item(src, size):
    return {"src": src, "dst": "", "new_name": os.path.basename(src),
            "category": "待确认", "confidence": 0.4, "reason": "low:0.40",
            "evidence": "", "action": "review", "kind": "document",
            "size": size, "ext": ".txt", "sha256": "", "tags": [],
            "review_kind": "low_conf", "file_id": None, "score": 0,
            "auto_exempt": False}


def test_store_plan_writes_and_refreshes_fingerprint(tmp_path):
    src_dir = tmp_path / "inbox"
    src_dir.mkdir()
    p = _write(str(src_dir / "a.txt"), "内容 A")
    db = str(tmp_path / "t.db")
    conn = DB.connect(db)
    DB.init_db(db)
    item = _review_item(p, os.path.getsize(p))
    CUR._store_plan(conn, "inbox", None, [item])
    row = DB.get_review_for_src(conn, p)
    assert row is not None
    assert row["payload"]["size"] == os.path.getsize(p)
    assert row["payload"]["mtime"] == os.path.getmtime(p)
    # 文件改动后重建计划：行仍只有一条，指纹跟上新 mtime
    time.sleep(0.02)
    _write(p, "内容 A 改动后更长")
    item2 = _review_item(p, os.path.getsize(p))
    CUR._store_plan(conn, "inbox", None, [item2])
    rows = conn.execute(
        "SELECT * FROM review_queue WHERE json_extract(payload, '$.src')=?",
        (p,)).fetchall()
    assert len(rows) == 1
    row2 = DB.get_review_for_src(conn, p)
    assert row2["payload"]["size"] == os.path.getsize(p)
    assert row2["payload"]["mtime"] == os.path.getmtime(p)
    conn.close()


def test_plan_files_skips_reviewed_before_extract(tmp_path):
    """文档引擎：已有复核行且未改动的文件不进 plan（正文抽取前已剔除）。"""
    root = tmp_path / "inbox"
    root.mkdir()
    kept = _write(str(root / "old.txt"),
                  "供货合同 甲方与乙方签订本供货合同，合同条款、协议内容详见正文，双方签章生效。")
    fresh = _write(str(root / "new.txt"),
                   "周报 本周工作总结：完成库存盘点，推进分仓建设，汇报重点工作完成情况。")
    db = str(tmp_path / "t.db")
    conn = DB.connect(db)
    DB.init_db(db)
    size, mtime = _fp(kept)
    DB.add_review(conn, plan_id=None, kind="low_conf",
                  payload={"src": kept, "size": size, "mtime": mtime})
    skipper = CUR._ReviewSkipper(conn)
    items, stats = plan_files(str(root), _load_rules(), no_ocr=True,
                              skip_reviewed=skipper.should_skip)
    srcs = [i["src"] for i in items]
    assert kept not in srcs
    assert fresh in srcs
    assert stats["review_skipped"] == 1
    conn.close()


def _media_args(tmp_path):
    cfg = {"library_root": str(tmp_path / "lib"),
           "inbox": str(tmp_path / "inbox"),
           "review_root": str(tmp_path / "lib" / "_Review"),
           "work_dir": str(tmp_path / "work"),
           "model_dir": str(tmp_path / "models"),
           "settle_minutes": 0,
           "thresholds": {"margin_min": 0.12, "score_min": 0.35, "blur_min": 50,
                          "phash_max_distance": 5, "near_dup_enabled": True},
           "photo_dirs": {}, "video_dirs": {},
           "review_dirs": {"待确认": "_Review/待确认"}}
    cfg_path = tmp_path / "media.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    return argparse.Namespace(media_config=str(cfg_path))


def _png(path, color=(200, 30, 30)):
    from PIL import Image
    Image.new("RGB", (64, 64), color).save(path)
    return path


def test_dispatch_plan_media_skips_reviewed(tmp_path):
    """媒体路径在 _collect_media_paths 之后过滤：已复核文件不进分类。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    old = _png(str(inbox / "old.png"))
    new = _png(str(inbox / "new.png"), color=(30, 200, 30))
    db = str(tmp_path / "t.db")
    conn = DB.connect(db)
    DB.init_db(db)
    size, mtime = _fp(old)
    DB.add_review(conn, plan_id=None, kind="low_conf",
                  payload={"src": old, "size": size, "mtime": mtime})
    args = _media_args(tmp_path)
    items, stats = CUR._dispatch_plan(str(inbox), "media", None, None, None,
                                      None, args, {}, learn_conn=conn)
    srcs = [i["src"] for i in items]
    assert old not in srcs
    assert new in srcs
    assert stats["review_skipped"] == 1
    conn.close()
