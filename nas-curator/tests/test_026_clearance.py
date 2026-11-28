#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0.2.6 回归：存量清仓（母本第二轮提案 2026-11-28，王震批准全做）。

P0 堵环（媒体扫描排除 holding 区 + moved_to 指纹 + plan 兜底）/
P1 blur 分流（截图类扩展名与小图跳过模糊判定，照片阈值 50→20）/
P3 扫描件文件名优先 + unsupported 单列 /
P4 reclassify order（默认 created_at DESC）+ batch_id 回滚 /
配置分发修复（ensure_config 合并补齐）/ 复核 payload 带 sha256。

只用临时目录与临时 DB，不碰项目 data/nas-curator.db 与 NAS。
运行：python -m pytest tests/test_026_clearance.py
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from common import db as DB
from common import routing as RT

THRESHOLDS = {"auto": 0.85, "auto_doc_llm": 0.75, "auto_media": 0.65,
              "auto_media_margin": 0.15, "review_suggest": 0.6}

ROOTS = {
    "work": "/vol1/1000/工作文件",
    "family": "/vol1/1000/家庭文件",
    "media": "/vol1/1000/媒体库",
    "inbox": "/vol1/1000/收件箱",
    "mac-backup": "/vol1/1000/下载/Wangzhen-Mac",
}


# ---------------------------------------------------------------- P0 堵环

def test_p0_collect_media_paths_skips_holding(tmp_path):
    """P0-A：_collect_media_paths 跳过 99-待确认（holding 区），与文档引擎口径一致。"""
    import curator
    inbox = tmp_path / "收件箱"
    holding = inbox / "99-待确认" / "06-重复待确认"
    holding.mkdir(parents=True)
    (holding / "dup.jpg").write_bytes(b"")
    (inbox / "normal.jpg").write_bytes(b"")
    (inbox / "sub").mkdir()
    (inbox / "sub" / "other.png").write_bytes(b"")
    got = curator._collect_media_paths(str(inbox))
    assert str(holding / "dup.jpg") not in got
    assert str(inbox / "normal.jpg") in got
    assert str(inbox / "sub" / "other.png") in got
    print("P0-A collect skips holding: PASS")


def test_p0_plan_media_files_skips_holding(tmp_path):
    """P0-C 兜底：plan_media_files 对 99-待确认 下的文件直接 skip/holding，不分类。"""
    from media_core.engine import plan_media_files
    holding = tmp_path / "收件箱" / "99-待确认" / "06-重复待确认"
    holding.mkdir(parents=True)
    p = holding / "dup.jpg"
    p.write_bytes(b"")
    items, stats = plan_media_files([str(p)], {})
    assert len(items) == 1, items
    assert items[0]["action"] == "skip" and items[0]["reason"] == "holding", items[0]
    assert stats["skipped"] == 1
    print("P0-C plan skips holding: PASS")


def test_p0_moved_to_fingerprint(tmp_path):
    """P0-B：批准 dup 移动后，新路径写进 payload.moved_to；已裁决指纹同时认
    原路径与新路径，同一文件不再产生第二条复核行。"""
    db = str(tmp_path / "t.db")
    DB.init_db(db)
    conn = DB.connect(db)
    pid = DB.create_plan(conn, "work", None, {"n": 1, "move": 0, "review": 1})
    rid = DB.add_review(conn, plan_id=pid, kind="dup", payload={
        "src": "/vol1/1000/收件箱/a.jpg", "category": "疑似重复",
        "confidence": 0.9, "reason": "dup:sha256", "suggested_dst": ""})
    conn.close()
    conn = DB.connect(db)
    assert DB.has_review_for_src(conn, "/vol1/1000/收件箱/a.jpg") is True
    assert DB.has_review_for_src(
        conn, "/vol1/1000/收件箱/99-待确认/06-重复待确认/a.jpg") is False
    DB.record_review_moved_to(
        conn, rid, "/vol1/1000/收件箱/99-待确认/06-重复待确认/a.jpg")
    # moved_to 后新路径也被认作已裁决；指纹集同时含原路径与新路径。
    assert DB.has_review_for_src(
        conn, "/vol1/1000/收件箱/99-待确认/06-重复待确认/a.jpg") is True
    fps = DB.list_reviewed_fingerprints(conn)
    assert "/vol1/1000/收件箱/a.jpg" in fps
    assert "/vol1/1000/收件箱/99-待确认/06-重复待确认/a.jpg" in fps
    conn.close()
    print("P0-B moved_to fingerprint: PASS")


# ---------------------------------------------------------------- P1 blur 分流

def test_p1_blur_split():
    """P1：截图类扩展名（png/gif/bmp/webp）与小图（长边<100px）跳过模糊判定；
    照片阈值 blur_min 由 50 下调到 20（config.json 与 engine 默认值同步）。"""
    from media_core.curate import decide, is_blur_check_applicable
    from media_core.engine import _DEFAULT_THRESHOLDS
    cfg = json.load(open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "media_core", "config.json"), encoding="utf-8"))
    assert cfg["thresholds"]["blur_min"] == 20, cfg["thresholds"]
    assert _DEFAULT_THRESHOLDS["blur_min"] == 20
    for ext in (".png", ".gif", ".bmp", ".webp", ".PNG"):
        assert is_blur_check_applicable(ext) is False, ext
    for ext in (".jpg", ".jpeg", ".heic", ".heif", ".tif"):
        assert is_blur_check_applicable(ext) is True, ext
    # 小图门控：长边 <100px 跳过；≥100px 不跳过；不传 long_edge 时不做门控。
    assert is_blur_check_applicable(".jpg", 50) is False
    assert is_blur_check_applicable(".jpg", 99) is False
    assert is_blur_check_applicable(".jpg", 100) is True
    assert is_blur_check_applicable(".jpg", None) is True
    # decide()：同为 blur=2 的照片，png（截图类）不判模糊、jpg 判模糊。
    th = {"margin_min": 0.12, "score_min": 0.35, "blur_min": 20}
    cfgd = {"thresholds": th}
    row_png = {"path": "/x/a.png", "name": "a.png", "kind": "照片",
               "size": 100, "mtime": 0, "ext": ".png"}
    row_jpg = {"path": "/x/b.jpg", "name": "b.jpg", "kind": "照片",
               "size": 100, "mtime": 0, "ext": ".jpg"}
    sc = {"zh": {"屏幕截图": 0.1, "文档与票据": 0.05},
          "sig": {"屏幕截图": 0.1, "文档与票据": 0.05},
          "sig_top": ("照片", 0.9), "sig_2": ("视频", 0.05)}
    cat, cat2, s1, s2, why = decide(row_png, sc, 2.0, 0, 0, 0, "", cfgd)
    assert why != "blurry", (cat, why)
    cat, cat2, s1, s2, why = decide(row_jpg, sc, 2.0, 0, 0, 0, "", cfgd)
    assert why == "blurry", (cat, why)
    # 小图 jpg（long_edge=50）也不判模糊。
    cat, cat2, s1, s2, why = decide(row_jpg, sc, 2.0, 0, 0, 0, "", cfgd,
                                    long_edge=50)
    assert why != "blurry", (cat, why)
    print("P1 blur split: PASS")


# ---------------------------------------------------------------- P3 文件名优先

def _scan_cats():
    return [
        {"path": "02-产品与注册", "keywords": ["产品注册证", "医疗器械注册证"],
         "exts": [".pdf"]},
        {"path": "03-合同与协议", "keywords": ["销售合同", "采购合同"],
         "exts": [".pdf"]},
        {"path": "09-会议与汇报", "keywords": ["会议纪要", "总办会"],
         "exts": [".pdf"]},
        {"path": "01-公司资质与证照", "keywords": ["营业执照"],
         "exts": [".pdf"]},
        {"path": "07-财务与经营分析", "keywords": ["发票"], "exts": [".pdf"]},
        {"path": "06-库存与物流", "keywords": ["报关单"], "exts": [".pdf"]},
        {"path": "12-招投标", "keywords": ["中标通知书"], "exts": [".pdf"]},
        {"path": "11-人事与行政", "keywords": ["离职证明"], "exts": [".pdf"]},
    ]


def test_p3_scan_name_category():
    """P3：扫描 PDF 先跑文件名优先定类——长词组命中直接定类；裸短词（合同/
    检验检疫证明等）走短词兜底；文件名无信号时返回空，不硬定类。"""
    from curator_core.engine import (SCAN_NAME_STRONG_KEYWORDS,
                                     UNSUPPORTED_EXTS, scan_name_category)
    cats = _scan_cats()
    # 长词组命中（best_category name_first，分 ≥8）。
    cat, sc, reason, ev = scan_name_category(
        "医疗器械注册证-ABC2024001.pdf", ".pdf", cats)
    assert cat == "02-产品与注册" and sc >= 8, (cat, sc, reason)
    # 短词兜底：裸“合同”命中不上长词组（销售合同/采购合同），走短词映射。
    cat, sc, reason, ev = scan_name_category("服务合同-2024.pdf", ".pdf", cats)
    assert cat == "03-合同与协议" and sc == 6, (cat, sc, reason)
    cat, sc, reason, ev = scan_name_category(
        "检验检疫证明-2024.pdf", ".pdf", cats)
    assert cat == "02-产品与注册" and sc == 18, (cat, sc, reason)
    # 无信号：医院+编号，返回空。
    cat, sc, reason, ev = scan_name_category(
        "营口市中心医院2024001.pdf", ".pdf", cats)
    assert cat == "" and sc == 0, (cat, sc)
    # unsupported 扩展名单列。
    for ext in (".exe", ".webloc", ".url", ".lnk", ".msi", ".apk"):
        assert ext in UNSUPPORTED_EXTS, ext
    assert ".pdf" not in UNSUPPORTED_EXTS and ".jpg" not in UNSUPPORTED_EXTS
    print("P3 scan-name category: PASS")


# ---------------------------------------------------------------- 复核 API（P4 order + batch_id）

@pytest.fixture()
def client(tmp_path, monkeypatch):
    import api.deps as deps
    work = tmp_path / "工作文件"
    family = tmp_path / "家庭文件"
    media = tmp_path / "媒体库"
    inbox = tmp_path / "收件箱"
    mac = tmp_path / "下载" / "Wangzhen-Mac"
    for d in (work, family, media, inbox, mac):
        d.mkdir(parents=True)
    libs = {"libraries": [
        {"id": "work", "name": "工作", "root": str(work)},
        {"id": "family", "name": "家庭", "root": str(family)},
        {"id": "media", "name": "媒体", "root": str(media)},
    ], "inbox": {"path": str(inbox)},
        "input_sources": [{"id": "mac-backup", "root": str(mac)}],
        "thresholds": dict(THRESHOLDS)}
    libs_path = tmp_path / "libraries.json"
    libs_path.write_text(json.dumps(libs, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(deps, "LIBRARIES_JSON", str(libs_path))
    db = str(tmp_path / "t.db")
    monkeypatch.setenv("CURATOR_DB", db)
    DB.init_db(db)
    conn = DB.connect(db)
    pid = DB.create_plan(conn, "work", None, {"n": 1, "move": 0, "review": 1})
    conn.close()
    from fastapi.testclient import TestClient
    from api.main import app
    with TestClient(app) as c:
        c._tmp = tmp_path
        c._db = db
        c._plan = pid
        yield c


def _add_review(c, kind, payload):
    conn = DB.connect(c._db)
    rid = DB.add_review(conn, plan_id=c._plan, kind=kind, payload=payload)
    conn.close()
    return rid


def _make_src(c, rel, content=b"x"):
    p = c._tmp / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return str(p)


def _set_created_at(c, rid, ts):
    conn = sqlite3.connect(c._db)
    conn.execute("UPDATE review_queue SET created_at=? WHERE id=?", (ts, rid))
    conn.commit()
    conn.close()


def test_p4_reclassify_order(client):
    """P4：reclassify 默认按 created_at DESC（最新行优先），order=asc 时按
    升序；不再只吃最老的前 1,000 条。"""
    rids = {}
    for label, ts in (("old", "2026-11-20T00:00:00"),
                      ("mid", "2026-11-26T00:00:00"),
                      ("new", "2026-11-28T00:00:00")):
        src = _make_src(client, "收件箱/%s.pdf" % label, label.encode())
        rid = _add_review(client, "low_conf", {
            "src": src, "category": "04-供应商与采购", "confidence": 0.45,
            "reason": "llm:0.80", "evidence": "", "suggested_dst": "",
            "tags": []})
        _set_created_at(client, rid, ts)
        rids[label] = rid
    r = client.post("/api/review/reclassify", json={})
    assert r.status_code == 200, r.text
    got = [x["id"] for x in r.json()["results"]]
    assert got == [rids["new"], rids["mid"], rids["old"]], got
    r = client.post("/api/review/reclassify", json={"order": "asc"})
    got = [x["id"] for x in r.json()["results"]]
    assert got == [rids["old"], rids["mid"], rids["new"]], got
    print("P4 reclassify order: PASS")


def test_p4_reclassify_apply_sets_batch_id(client):
    """P4：reclassify apply=true 时移动打 batch_id，operations.batch_id 可查，
    回滚时可只回滚该批（不连带同 plan 旧移动）。"""
    from common import rollback as RB
    src = _make_src(client, "收件箱/g.pdf", b"g")
    rid = _add_review(client, "low_conf", {
        "src": src, "category": "04-供应商与采购", "confidence": 0.45,
        "reason": "llm:0.80", "evidence": "", "suggested_dst": "",
        "tags": []})
    r = client.post("/api/review/reclassify", json={"apply": True})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["moved"] == 1 and d["batch_id"], d
    dst = str(client._tmp / "工作文件" / "04-供应商与采购" / "g.pdf")
    assert os.path.exists(dst) and not os.path.exists(src)
    # batch_id 可查到该次移动；按批回滚只回滚该批。
    conn = DB.connect(client._db)
    ops = DB.list_operations_by_batch(conn, d["batch_id"])
    assert len(ops) == 1 and ops[0]["src_path"] == src \
        and ops[0]["dst_path"] == dst, ops
    res = RB.rollback_from_batch(conn, d["batch_id"])
    assert res["moved"] == 1 and not res["failed"] and not res["missing"], res
    conn.close()
    assert os.path.exists(src) and not os.path.exists(dst)
    print("P4 batch_id rollback: PASS")


# ---------------------------------------------------------------- 配置分发修复

def test_config_merge_missing_keys(tmp_path, monkeypatch):
    """配置分发修复：ensure_config 在卷配置存在但缺键时，把镜像自带配置里
    缺失的键合并进来（只加不改、不删）；用户已有值一律保留。"""
    import api.deps as deps
    shipped_dir = tmp_path / "shipped"
    volume_dir = tmp_path / "volume"
    shipped_dir.mkdir()
    volume_dir.mkdir()
    shipped = {"libraries": [], "thresholds": {
        "auto": 0.85, "auto_doc_llm": 0.75, "auto_media": 0.65,
        "auto_media_margin": 0.15, "review_suggest": 0.6},
        "new_key_026": True}
    (shipped_dir / "libraries.json").write_text(
        json.dumps(shipped, ensure_ascii=False), encoding="utf-8")
    volume = {"libraries": [], "thresholds": {"auto": 0.85},
              "my_custom": "keep-me"}
    (volume_dir / "libraries.json").write_text(
        json.dumps(volume, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(deps, "SHIPPED_CONFIG_DIR", str(shipped_dir))
    monkeypatch.setattr(deps, "CONFIG_DIR", str(volume_dir))
    monkeypatch.setattr(deps, "LIBRARIES_JSON",
                        str(volume_dir / "libraries.json"))
    deps.ensure_config()
    got = json.loads((volume_dir / "libraries.json").read_text(
        encoding="utf-8"))
    assert got["thresholds"]["auto"] == 0.85
    assert got["thresholds"]["auto_doc_llm"] == 0.75
    assert got["thresholds"]["auto_media"] == 0.65
    assert got["thresholds"]["auto_media_margin"] == 0.15
    assert got["thresholds"]["review_suggest"] == 0.6
    assert got["new_key_026"] is True
    assert got["my_custom"] == "keep-me"
    # _merge_missing 不覆盖已有值。
    dst = {"a": 1, "nested": {"x": 9}}
    assert deps._merge_missing(dst, {"a": 2, "b": 3, "nested": {"y": 4}}) is True
    assert dst == {"a": 1, "b": 3, "nested": {"x": 9, "y": 4}}
    assert deps._merge_missing(dst, {"a": 1, "b": 3, "nested": {"x": 9}}) is False
    print("config merge missing keys: PASS")


def test_config_ensure_copies_when_missing(tmp_path, monkeypatch):
    """卷配置完全缺失时仍按旧行为从镜像复制（不回归）。"""
    import api.deps as deps
    shipped_dir = tmp_path / "shipped"
    volume_dir = tmp_path / "volume"
    shipped_dir.mkdir()
    volume_dir.mkdir()
    (shipped_dir / "libraries.json").write_text(
        json.dumps({"libraries": [], "thresholds": {"auto": 0.85}},
                   ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(deps, "SHIPPED_CONFIG_DIR", str(shipped_dir))
    monkeypatch.setattr(deps, "CONFIG_DIR", str(volume_dir))
    monkeypatch.setattr(deps, "LIBRARIES_JSON",
                        str(volume_dir / "libraries.json"))
    deps.ensure_config()
    assert (volume_dir / "libraries.json").exists()
    got = json.loads((volume_dir / "libraries.json").read_text(
        encoding="utf-8"))
    assert got["thresholds"]["auto"] == 0.85
    print("config ensure copies when missing: PASS")


# ---------------------------------------------------------------- sha256 / batch 回滚

def test_store_plan_payload_has_sha256(tmp_path):
    """复核 payload 带 sha256：_store_plan 写 review 行时把 plan-item 的
    sha256 带进 payload，批准移动写 operations 时不再为空。"""
    import curator
    db = str(tmp_path / "t.db")
    DB.init_db(db)
    conn = DB.connect(db)
    items = [{
        "src": "/vol1/1000/收件箱/h.pdf", "dst": "", "new_name": "h.pdf",
        "category": "04-供应商与采购", "confidence": 0.45,
        "confidence_original": 0.80, "reason": "llm:0.80", "evidence": "",
        "action": "review", "kind": "document", "size": 10, "ext": ".pdf",
        "sha256": "a" * 64, "tags": [], "review_kind": "low_conf",
        "file_id": None, "score": 0, "auto_exempt": False,
    }]
    curator._store_plan(conn, "work", "r1", items)
    row = conn.execute(
        "SELECT payload FROM review_queue").fetchone()
    payload = json.loads(row[0])
    assert payload["sha256"] == "a" * 64, payload
    conn.close()
    print("store_plan payload sha256: PASS")


def test_batch_id_rollback_only_that_batch(tmp_path):
    """batch_id 回滚只回滚该批：另一批的移动不受影响。"""
    from common import rollback as RB
    db = str(tmp_path / "t.db")
    DB.init_db(db)
    conn = DB.connect(db)
    pid = DB.create_plan(conn, "work", None, {"n": 2, "move": 2, "review": 0})
    a_src = tmp_path / "a.txt"
    a_dst = tmp_path / "a-moved.txt"
    b_src = tmp_path / "b.txt"
    b_dst = tmp_path / "b-moved.txt"
    a_src.write_text("a")
    b_src.write_text("b")
    os.rename(a_src, a_dst)
    os.rename(b_src, b_dst)
    DB.add_operation(conn, pid, str(a_src), str(a_dst), "1" * 64,
                     batch_id="batch-A")
    DB.add_operation(conn, pid, str(b_src), str(b_dst), "2" * 64,
                     batch_id="batch-B")
    res = RB.rollback_from_batch(conn, "batch-A")
    assert res["moved"] == 1 and not res["failed"] and not res["missing"], res
    assert a_src.exists() and not a_dst.exists()
    assert b_dst.exists() and not b_src.exists()
    conn.close()
    print("batch rollback only that batch: PASS")


def test_027_init_db_migrates_old_operations_table(tmp_path):
    """0.2.7 回归：0.2.6 的 schema.sql 在旧库上建 idx_operations_batch 会报
    "no such column: batch_id"（旧 operations 表本无此列），init_db 整体失败、
    容器起不来（0.2.6 首轮部署即因此回滚）。修复后 init_db 在旧库上应成功：
    补列 + 建索引，且幂等。"""
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=OFF")
    # 0.2.5 时代的 operations 表：有 decided_by，无 batch_id。
    conn.execute("""
        CREATE TABLE operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER REFERENCES plans (id),
            src_path TEXT NOT NULL,
            dst_path TEXT NOT NULL,
            sha256 TEXT,
            decided_by TEXT,
            executed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
            rolled_back_at TEXT
        )""")
    conn.execute("CREATE INDEX idx_operations_plan ON operations (plan_id)")
    conn.commit()
    conn.close()
    DB.init_db(db)  # 旧库上不应抛错
    conn = DB.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(operations)")]
    assert "batch_id" in cols, cols
    assert "decided_by" in cols
    idx = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='operations'")]
    assert "idx_operations_batch" in idx, idx
    pid = DB.create_plan(conn, "work", None, {"n": 0, "move": 0, "review": 0})
    DB.add_operation(conn, pid, "/a", "/b", "x" * 64, batch_id="t1")
    ops = DB.list_operations_by_batch(conn, "t1")
    assert len(ops) == 1 and ops[0]["src_path"] == "/a"
    conn.close()
    DB.init_db(db)  # 幂等：再跑一次不报错、不丢列
    conn = DB.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(operations)")]
    assert "batch_id" in cols
    conn.close()
    print("init_db migrates old operations table: PASS")
