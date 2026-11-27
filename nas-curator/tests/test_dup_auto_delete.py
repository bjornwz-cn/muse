#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-11-27 提速改动 1 回归：精确重复（dup:sha256）自动删除。

覆盖：保留份三重验证（库内存在 + SHA 一致 + 可解码）全过才删；
保留份缺失/在收件箱内/哈希不符/大小不符/解不开 → 留复核；
绝不删保留份、绝不删库内文件作副本；evidence 旧址已空时经 operations 表
解析目的地；删除纪律（先写清单、再备份 DB、operations 行注明 decided_by、
复核行判 approved）；回滚跳过删除行；预演不删。
只用 tmp 目录与临时 DB，不碰 NAS、不碰生产库。
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from common import db as DB  # noqa: E402
from common import dup_delete as DD  # noqa: E402
from common import rollback as RB  # noqa: E402
from common.hashing import sha256_file  # noqa: E402


def _roots(tmp):
    roots = {}
    for top in ("work", "family", "media", "inbox"):
        d = os.path.join(tmp, top)
        os.makedirs(d, exist_ok=True)
        roots[top] = d
    return roots


def _png(path, color=(200, 30, 30)):
    from PIL import Image
    Image.new("RGB", (64, 64), color).save(path)
    return path


def _txt(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _dup_item(src, evidence, sha, size, ext=".png"):
    return {"src": src, "dst": "", "new_name": os.path.basename(src),
            "category": "疑似重复", "confidence": 0.9, "reason": "dup:sha256",
            "evidence": evidence, "action": "review", "kind": "media",
            "size": size, "ext": ext, "sha256": sha, "tags": [],
            "review_kind": "dup", "file_id": None, "score": 0,
            "auto_exempt": False}


def test_verify_kept_copy_ok(tmp_path):
    roots = _roots(str(tmp_path))
    kept = _png(os.path.join(roots["media"], "a.png"))
    copy = _png(os.path.join(roots["inbox"], "a-copy.png"))
    sha = sha256_file(copy)
    ok, why = DD.verify_kept_copy(copy, kept, sha, os.path.getsize(copy),
                                  roots, ext=".png")
    assert (ok, why) == (True, "ok")


def test_verify_kept_missing(tmp_path):
    roots = _roots(str(tmp_path))
    copy = _png(os.path.join(roots["inbox"], "a-copy.png"))
    sha = sha256_file(copy)
    ok, why = DD.verify_kept_copy(copy, os.path.join(roots["media"], "gone.png"),
                                  sha, os.path.getsize(copy), roots, ext=".png")
    assert (ok, why) == (False, "kept-missing")


def test_verify_kept_in_inbox_not_deleted(tmp_path):
    """保留份也在收件箱：两个都在收件箱时不自动删，防链式误删。"""
    roots = _roots(str(tmp_path))
    first = _png(os.path.join(roots["inbox"], "a.png"))
    copy = _png(os.path.join(roots["inbox"], "a-copy.png"))
    sha = sha256_file(copy)
    ok, why = DD.verify_kept_copy(copy, first, sha, os.path.getsize(copy),
                                  roots, ext=".png")
    assert (ok, why) == (False, "kept-in-inbox")


def test_verify_kept_sha_mismatch(tmp_path):
    roots = _roots(str(tmp_path))
    kept = _txt(os.path.join(roots["work"], "a.txt"), "aaa")
    copy = _txt(os.path.join(roots["inbox"], "a-copy.txt"), "bbb")
    sha = sha256_file(copy)
    ok, why = DD.verify_kept_copy(copy, kept, sha, os.path.getsize(copy),
                                  roots, ext=".txt")
    assert (ok, why) == (False, "sha-mismatch")


def test_verify_kept_size_mismatch(tmp_path):
    roots = _roots(str(tmp_path))
    kept = _txt(os.path.join(roots["work"], "a.txt"), "aaa")
    copy = _txt(os.path.join(roots["inbox"], "a-copy.txt"), "aaaa")
    sha = sha256_file(copy)
    ok, why = DD.verify_kept_copy(copy, kept, sha, os.path.getsize(copy),
                                  roots, ext=".txt")
    assert (ok, why) == (False, "size-mismatch")


def test_verify_kept_undecodable_photo(tmp_path):
    """保留份是坏图（PIL 打不开）→ 不删。"""
    roots = _roots(str(tmp_path))
    kept = os.path.join(roots["media"], "bad.png")
    with open(kept, "wb") as f:
        f.write(os.urandom(64))
    copy = os.path.join(roots["inbox"], "bad-copy.png")
    with open(copy, "wb") as f:
        f.write(open(kept, "rb").read())
    sha = sha256_file(copy)
    ok, why = DD.verify_kept_copy(copy, kept, sha, os.path.getsize(copy),
                                  roots, ext=".png")
    assert (ok, why) == (False, "kept-undecodable")


def test_verify_kept_rmvb_undecodable(tmp_path):
    """保留份是 .rmvb：RealVideo 黑名单，不尝试解码 → 不删。"""
    roots = _roots(str(tmp_path))
    kept = os.path.join(roots["media"], "v.rmvb")
    with open(kept, "wb") as f:
        f.write(os.urandom(128))
    copy = os.path.join(roots["inbox"], "v-copy.rmvb")
    with open(copy, "wb") as f:
        f.write(open(kept, "rb").read())
    sha = sha256_file(copy)
    ok, why = DD.verify_kept_copy(copy, kept, sha, os.path.getsize(copy),
                                  roots, ext=".rmvb")
    assert (ok, why) == (False, "kept-undecodable")


def test_verify_src_not_in_inbox(tmp_path):
    """副本本身在库内时绝不当作待删副本。"""
    roots = _roots(str(tmp_path))
    kept = _png(os.path.join(roots["media"], "a.png"))
    copy = _png(os.path.join(roots["media"], "a-copy.png"))
    sha = sha256_file(copy)
    ok, why = DD.verify_kept_copy(copy, kept, sha, os.path.getsize(copy),
                                  roots, ext=".png")
    assert (ok, why) == (False, "src-not-in-inbox")


def test_verify_video_ok_with_fake_cv2(tmp_path, monkeypatch):
    """视频保留份：cv2 读到一帧即算可解码（本机无 cv2，用桩模块）。"""
    import types as _types
    mod = _types.ModuleType("cv2")

    class _Cap:
        def __init__(self, path):
            self.path = path
        def read(self):
            return (True, object())
        def release(self):
            pass

    mod.VideoCapture = _Cap
    monkeypatch.setitem(sys.modules, "cv2", mod)
    roots = _roots(str(tmp_path))
    kept = os.path.join(roots["media"], "v.mp4")
    with open(kept, "wb") as f:
        f.write(os.urandom(256))
    copy = os.path.join(roots["inbox"], "v-copy.mp4")
    with open(copy, "wb") as f:
        f.write(open(kept, "rb").read())
    sha = sha256_file(copy)
    ok, why = DD.verify_kept_copy(copy, kept, sha, os.path.getsize(copy),
                                  roots, ext=".mp4")
    assert (ok, why) == (True, "ok")


def test_resolve_kept_path_direct(tmp_path):
    roots = _roots(str(tmp_path))
    kept = _png(os.path.join(roots["media"], "a.png"))
    conn = DB.connect(str(tmp_path / "t.db"))
    DB.init_db(str(tmp_path / "t.db"))
    assert DD.resolve_kept_path(conn, kept) == os.path.abspath(kept)
    conn.close()


def test_resolve_kept_path_via_operations(tmp_path):
    """evidence 旧址已空：经 operations 表找到被移走的目的地（含一跳链式）。"""
    roots = _roots(str(tmp_path))
    kept = _png(os.path.join(roots["media"], "a.png"))
    old_inbox = os.path.join(roots["inbox"], "a.png")
    mid = os.path.join(roots["work"], "moved-a.png")
    with open(mid, "wb") as f:
        f.write(open(kept, "rb").read())
    conn = DB.connect(str(tmp_path / "t.db"))
    DB.init_db(str(tmp_path / "t.db"))
    plan_id = DB.create_plan(conn, "inbox", None, {"n": 1})
    DB.add_operation(conn, plan_id, old_inbox, mid, sha256_file(mid))
    DB.add_operation(conn, plan_id, mid, kept, sha256_file(kept))
    assert DD.resolve_kept_path(conn, old_inbox) == os.path.abspath(kept)
    conn.close()


def test_resolve_kept_path_rolled_back_returns_none(tmp_path):
    roots = _roots(str(tmp_path))
    kept = _png(os.path.join(roots["media"], "a.png"))
    old_inbox = os.path.join(roots["inbox"], "a.png")
    conn = DB.connect(str(tmp_path / "t.db"))
    DB.init_db(str(tmp_path / "t.db"))
    plan_id = DB.create_plan(conn, "inbox", None, {"n": 1})
    op_id = DB.add_operation(conn, plan_id, old_inbox, kept, sha256_file(kept))
    DB.mark_rolled_back(conn, op_id)
    assert DD.resolve_kept_path(conn, old_inbox) is None
    conn.close()


def _setup_driver_case(tmp_path):
    """返回 (roots, conn, db_path, plan_id, kept, copy, item)。"""
    roots = _roots(str(tmp_path))
    kept = _png(os.path.join(roots["media"], "a.png"))
    copy = _png(os.path.join(roots["inbox"], "a-copy.png"))
    sha = sha256_file(copy)
    size = os.path.getsize(copy)
    db_path = str(tmp_path / "t.db")
    conn = DB.connect(db_path)
    DB.init_db(db_path)
    plan_id = DB.create_plan(conn, "inbox", None, {"n": 1})
    item = _dup_item(copy, kept, sha, size)
    DB.add_plan_item(conn, plan_id, item)
    DB.add_review(conn, plan_id=plan_id, kind="dup",
                  payload={"src": copy, "category": "疑似重复",
                           "confidence": 0.9, "reason": "dup:sha256",
                           "evidence": kept, "size": size, "mtime": None})
    return roots, conn, db_path, plan_id, kept, copy, item


def test_delete_end_to_end(tmp_path):
    roots, conn, db_path, plan_id, kept, copy, item = _setup_driver_case(tmp_path)
    stats = DD.delete_exact_dup_copies(
        [item], conn, plan_id, roots,
        manifest_dir=str(tmp_path / "manifests"),
        backup_dir=str(tmp_path / "backups"), dry_run=False)
    assert stats["deleted"] == 1, stats
    assert stats["candidates"] == 1
    assert not os.path.exists(copy)  # 副本已删
    assert os.path.exists(kept)  # 保留份完好
    # 清单：逐名存盘，含 src/保留份/sha/大小
    manifest = stats["manifest"]
    assert manifest and os.path.isfile(manifest)
    lines = [json.loads(l) for l in open(manifest, encoding="utf-8")]
    assert len(lines) == 1
    assert lines[0]["src"] == copy and lines[0]["kept"] == kept
    assert lines[0]["sha256"] == sha256_file(kept)
    assert lines[0]["size"] == os.path.getsize(kept)
    # DB 备份已写
    assert stats["backup"] and os.path.isfile(stats["backup"])
    # operations 行：dst="<deleted>"、decided_by 注明
    ops = DB.list_operations(conn, plan_id)
    assert len(ops) == 1
    assert ops[0]["src_path"] == copy and ops[0]["dst_path"] == DD.DELETED_DST
    assert ops[0]["decided_by"] == DD.DECIDED_BY
    # 复核行判为 approved（同一 decided_by）
    row = DB.get_review_for_src(conn, copy)
    assert row["status"] == "approved" and row["decided_by"] == DD.DECIDED_BY
    # plan_items 行置 done
    pi = DB.list_plan_items(conn, plan_id)
    assert pi[0]["status"] == "done"
    conn.close()


def test_delete_dry_run_deletes_nothing(tmp_path):
    roots, conn, db_path, plan_id, kept, copy, item = _setup_driver_case(tmp_path)
    stats = DD.delete_exact_dup_copies(
        [item], conn, plan_id, roots,
        manifest_dir=str(tmp_path / "manifests"),
        backup_dir=str(tmp_path / "backups"), dry_run=True)
    assert stats["would_delete"] == 1 and stats["deleted"] == 0
    assert stats["manifest"] == "" and stats["backup"] == ""
    assert os.path.exists(copy) and os.path.exists(kept)
    assert DB.list_operations(conn, plan_id) == []
    conn.close()


def test_delete_skips_when_kept_in_inbox(tmp_path):
    """两个都在收件箱：不删，副本与复核行都在。"""
    roots = _roots(str(tmp_path))
    first = _png(os.path.join(roots["inbox"], "a.png"))
    copy = _png(os.path.join(roots["inbox"], "a-copy.png"))
    sha = sha256_file(copy)
    db_path = str(tmp_path / "t.db")
    conn = DB.connect(db_path)
    DB.init_db(db_path)
    plan_id = DB.create_plan(conn, "inbox", None, {"n": 1})
    item = _dup_item(copy, first, sha, os.path.getsize(copy))
    stats = DD.delete_exact_dup_copies(
        [item], conn, plan_id, roots,
        manifest_dir=str(tmp_path / "manifests"),
        backup_dir=str(tmp_path / "backups"), dry_run=False)
    assert stats["deleted"] == 0
    assert stats["skipped"].get("kept-in-inbox") == 1, stats
    assert os.path.exists(copy) and os.path.exists(first)
    assert stats["manifest"] == ""  # 无可删项就不写清单/备份
    conn.close()


def test_delete_skips_when_src_changed_after_plan(tmp_path):
    """plan 与 apply 之间副本被改过：删前重算 SHA 不符 → 跳过。"""
    roots, conn, db_path, plan_id, kept, copy, item = _setup_driver_case(tmp_path)
    _png(copy, color=(10, 200, 10))  # 改写副本，SHA 变了
    stats = DD.delete_exact_dup_copies(
        [item], conn, plan_id, roots,
        manifest_dir=str(tmp_path / "manifests"),
        backup_dir=str(tmp_path / "backups"), dry_run=False)
    assert stats["deleted"] == 0
    assert stats["skipped"].get("src-changed") == 1, stats
    assert os.path.exists(copy)
    conn.close()


def test_delete_resolves_evidence_via_operations(tmp_path):
    """同批先出现的副本已归位：evidence 旧址已空，经 operations 解析到库内目的地后删。"""
    roots = _roots(str(tmp_path))
    kept = _png(os.path.join(roots["media"], "a.png"))
    old_inbox = os.path.join(roots["inbox"], "a.png")
    copy = _png(os.path.join(roots["inbox"], "a-copy.png"))
    sha = sha256_file(copy)
    db_path = str(tmp_path / "t.db")
    conn = DB.connect(db_path)
    DB.init_db(db_path)
    plan_id = DB.create_plan(conn, "inbox", None, {"n": 1})
    DB.add_operation(conn, plan_id, old_inbox, kept, sha)
    item = _dup_item(copy, old_inbox, sha, os.path.getsize(copy))
    stats = DD.delete_exact_dup_copies(
        [item], conn, plan_id, roots,
        manifest_dir=str(tmp_path / "manifests"),
        backup_dir=str(tmp_path / "backups"), dry_run=False)
    assert stats["deleted"] == 1, stats
    assert not os.path.exists(copy) and os.path.exists(kept)
    conn.close()


def test_rollback_skips_deleted_rows(tmp_path):
    """回滚跳过删除行，只移回正常移动行。"""
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    moved_src = src_dir / "m.txt"
    moved_src.write_text("m", encoding="utf-8")
    moved_dst = dst_dir / "m.txt"
    moved_dst.write_text("m", encoding="utf-8")
    db_path = str(tmp_path / "t.db")
    conn = DB.connect(db_path)
    DB.init_db(db_path)
    plan_id = DB.create_plan(conn, "inbox", None, {"n": 2})
    DB.add_operation(conn, plan_id, str(moved_src), str(moved_dst), "x")
    DB.add_operation(conn, plan_id, "/tmp/inbox/gone.png", DD.DELETED_DST, "y",
                     decided_by=DD.DECIDED_BY)
    res = RB.rollback_from_operations(conn, plan_id)
    assert res["moved"] == 1, res
    assert res["missing"] == [] and res["failed"] == []
    assert os.path.exists(str(moved_src))  # 移回了
    assert not os.path.exists(str(moved_dst))
    ops = DB.list_operations(conn, plan_id)
    deleted = [o for o in ops if o["dst_path"] == DD.DELETED_DST][0]
    assert deleted["rolled_back_at"] is None  # 删除行不标记回滚
    conn.close()


def test_add_operation_decided_by(tmp_path):
    db_path = str(tmp_path / "t.db")
    conn = DB.connect(db_path)
    DB.init_db(db_path)
    plan_id = DB.create_plan(conn, "inbox", None, {})
    op_id = DB.add_operation(conn, plan_id, "/a", "/b", "s", decided_by="tester")
    row = conn.execute("SELECT * FROM operations WHERE id=?",
                       (op_id,)).fetchone()
    assert row["decided_by"] == "tester"
    # 旧调用（不传 decided_by）仍可用
    op2 = DB.add_operation(conn, plan_id, "/a2", "/b2", "s2")
    row2 = conn.execute("SELECT * FROM operations WHERE id=?",
                        (op2,)).fetchone()
    assert row2["decided_by"] is None
    conn.close()


def test_apply_items_manifest_next_to_db(tmp_path, monkeypatch):
    """复核补查：_apply_items 的删除清单与 DB 备份必须落 DB 同目录（生产上是
    持久卷 /data），不能落容器内 /app/data（重建即丢）。"""
    import argparse

    import curator as CUR

    roots = _roots(str(tmp_path))
    kept = _txt(os.path.join(roots["media"], "keep.txt"), "hello dup")
    copy = _txt(os.path.join(roots["inbox"], "copy.txt"), "hello dup")
    sha = sha256_file(copy)
    size = os.path.getsize(copy)
    db_path = str(tmp_path / "t.db")
    conn = DB.connect(db_path)
    DB.init_db(db_path)
    plan_id = DB.create_plan(conn, "inbox", None, {"n": 1})
    DB.add_review(conn, file_id=None, plan_id=plan_id, kind="dup",
                  payload={"src": copy, "category": "疑似重复",
                           "confidence": 0.9, "reason": "dup:sha256",
                           "evidence": kept, "suggested_dst": "", "tags": []})
    cfg = {"libraries": [{"id": "work", "root": roots["work"]},
                         {"id": "family", "root": roots["family"]},
                         {"id": "media", "root": roots["media"]}],
           "inbox": {"path": roots["inbox"]},
           "thresholds": {"auto": 0.85}}
    monkeypatch.setattr(CUR, "_load_libraries", lambda: cfg)
    args = argparse.Namespace(dry_run=False, map="", report="", uid=1000,
                              gid=1000, db=db_path)
    item = _dup_item(copy, kept, sha, size, ext=".txt")
    report = CUR._apply_items([item], roots["inbox"], args, conn, plan_id)
    assert not os.path.exists(copy), "副本应已删"
    assert os.path.exists(kept), "保留份绝不能动"
    assert report.get("moved", 0) == 0
    manifest_dir = os.path.join(str(tmp_path), "dup_deletions")
    backup_dir = os.path.join(str(tmp_path), "backups")
    manifests = os.listdir(manifest_dir) if os.path.isdir(manifest_dir) else []
    backups = os.listdir(backup_dir) if os.path.isdir(backup_dir) else []
    assert len(manifests) == 1 and manifests[0].startswith("dup-delete-"), manifests
    assert len(backups) == 1 and backups[0].startswith("nas-curator.db.bak-"), backups
    row = DB.get_review_for_src(conn, copy)
    assert row is not None and row["status"] == "approved", row
    conn.close()
