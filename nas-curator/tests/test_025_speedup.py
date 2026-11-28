#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0.2.5 回归：分模态阈值 / 批量批准修法 / review_suggest / 杂项分流 /
EXIF 年月归档 / 重分类接口 / 分页（母本提案 2026-11-28，王震批准全做）。

只用临时目录与临时 DB，不碰项目 data/nas-curator.db 与 NAS。
运行：python -m pytest tests/test_025_speedup.py
"""
import json
import os
import struct
import sys
import datetime
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

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


# ---------------------------------------------------------------- §3.1 分模态阈值

def test_auto_threshold_for_per_modality():
    assert RT.auto_threshold_for(
        {"reason": "llm:0.70", "kind": "document", "ext": ".xls"},
        THRESHOLDS) == 0.75
    assert RT.auto_threshold_for(
        {"reason": "siglip:0.80", "kind": "media", "ext": ".jpg"},
        THRESHOLDS) == 0.65
    assert RT.auto_threshold_for(
        {"reason": "rule+sem:0.85", "kind": "document", "ext": ".pdf"},
        THRESHOLDS) == 0.85
    # 规则分文档兜底 0.85 不动；缺配置时用默认值。
    assert RT.auto_threshold_for(
        {"reason": "rule:0.9", "kind": "document", "ext": ".pdf"}, {}) == 0.85
    assert RT.auto_threshold_for(
        {"reason": "llm:0.70", "kind": "document", "ext": ".xls"}, {}) == 0.75
    assert RT.auto_threshold_for(
        {"reason": "siglip:0.80", "kind": "media", "ext": ".jpg"}, {}) == 0.65
    # EXIF 定位置放是确定性结论，恒可自动。
    assert RT.auto_threshold_for(
        {"reason": "exif:2024-03;siglip:0.40", "kind": "media", "ext": ".jpg"},
        THRESHOLDS) == 0.0
    assert RT.auto_threshold_for(
        {"reason": "siglip:0.40", "kind": "media", "ext": ".jpg",
         "exif_placed": True}, THRESHOLDS) == 0.0
    print("auto_threshold_for: PASS")


def test_is_auto_apply_eligible_per_modality():
    E = RT.is_auto_apply_eligible
    kw = {"thresholds": THRESHOLDS}
    # LLM 文档 0.80 可自动，0.70 不可（门槛 0.75）。
    assert E({"action": "move", "kind": "document", "ext": ".xls",
              "confidence": 0.80, "reason": "llm:0.80"}, **kw) is True
    assert E({"action": "move", "kind": "document", "ext": ".xls",
              "confidence": 0.70, "reason": "llm:0.70"}, **kw) is False
    # 媒体 0.70 + margin 0.69 可自动；margin 0.05 不可。
    assert E({"action": "move", "kind": "media", "ext": ".jpg",
              "confidence": 0.70, "reason": "siglip:0.70",
              "evidence": "A 0.70 / B 0.01"}, **kw) is True
    assert E({"action": "move", "kind": "media", "ext": ".jpg",
              "confidence": 0.70, "reason": "siglip:0.70",
              "evidence": "A 0.70 / B 0.65"}, **kw) is False
    # 媒体 0.60 不可（门槛 0.65）；规则分文档 0.85 仍是兜底。
    assert E({"action": "move", "kind": "media", "ext": ".jpg",
              "confidence": 0.60, "reason": "siglip:0.60",
              "evidence": "A 0.60 / B 0.01"}, **kw) is False
    assert E({"action": "move", "kind": "document", "ext": ".pdf",
              "confidence": 0.85, "reason": "rule+sem:0.85"}, **kw) is True
    assert E({"action": "move", "kind": "document", "ext": ".pdf",
              "confidence": 0.84, "reason": "rule+sem:0.84"}, **kw) is False
    # EXIF 定位置放：内容置信度低也自动（归位不依赖内容判断）。
    assert E({"action": "move", "kind": "media", "ext": ".jpg",
              "confidence": 0.40, "reason": "exif:2024-03;low:0.40",
              "evidence": "A 0.40 / B 0.35"}, **kw) is True
    # unit 整目录永不自动；向后兼容：显式传浮点阈值仍优先。
    assert E({"action": "move", "kind": "unit", "ext": "(目录)",
              "confidence": 0.95}, **kw) is False
    assert E({"action": "move", "kind": "document", "ext": ".txt",
              "confidence": 0.9}, 0.85) is True
    assert E({"action": "move", "kind": "document", "ext": ".txt",
              "confidence": 0.9}, 0.95) is False
    print("is_auto_apply_eligible per-modality: PASS")


def test_guard_inbox_keeps_dst_and_confidence():
    # 收件箱 -> 工作：低于门槛转 review，保留 dst、不压置信度。
    items = [{"src": "/vol1/1000/收件箱/x.pdf",
              "dst": "/vol1/1000/工作文件/04-供应商与采购/x.pdf",
              "action": "move", "category": "04-供应商与采购",
              "confidence": 0.70, "reason": "llm:0.70", "review_kind": None}]
    out, info = RT.apply_path_prior_guard(items, ROOTS, thresholds=THRESHOLDS)
    assert info["guarded"] == 1 and info["kept_dst"] == 1
    assert out[0]["action"] == "review"
    assert out[0]["dst"] == "/vol1/1000/工作文件/04-供应商与采购/x.pdf"
    assert out[0]["confidence"] == 0.70
    # 达到分模态阈值（LLM 0.80 >= 0.75）-> 不拦。
    items = [{"src": "/vol1/1000/收件箱/x.pdf",
              "dst": "/vol1/1000/工作文件/04-供应商与采购/x.pdf",
              "action": "move", "category": "04-供应商与采购",
              "confidence": 0.80, "reason": "llm:0.80", "review_kind": None}]
    out, info = RT.apply_path_prior_guard(items, ROOTS, thresholds=THRESHOLDS)
    assert info["guarded"] == 0 and out[0]["action"] == "move"
    # 库 -> 库跨顶层：低于门槛仍转 review，但 0.2.5 起保留 dst（只压置信度）。
    items = [{"src": "/vol1/1000/家庭文件/刘音/x.pdf",
              "dst": "/vol1/1000/工作文件/04-供应商与采购/x.pdf",
              "action": "move", "category": "04-供应商与采购",
              "confidence": 0.70, "reason": "llm:0.70", "review_kind": None}]
    out, info = RT.apply_path_prior_guard(items, ROOTS, thresholds=THRESHOLDS)
    assert info["guarded"] == 1 and info["kept_dst"] == 0
    assert out[0]["action"] == "review"
    assert out[0]["dst"] == "/vol1/1000/工作文件/04-供应商与采购/x.pdf"
    assert out[0]["confidence"] == 0.45
    print("guard inbox keeps dst: PASS")


def test_recover_confidence_original():
    assert RT.recover_confidence_original(
        {"confidence": 0.45, "confidence_original": 0.80}) == 0.80
    assert RT.recover_confidence_original(
        {"confidence": 0.45, "reason": "llm:0.70;path-prior:低置信0.70不跨顶层",
         "evidence": "x"}) == 0.70
    assert RT.recover_confidence_original(
        {"confidence": 0.797, "reason": "siglip:0.80",
         "evidence": "A 0.80 / B 0.01"}) == 0.80
    assert RT.recover_confidence_original(
        {"confidence": 0.44, "reason": "low:margin:0.04",
         "evidence": "文档与票据 0.44"}) == 0.44
    assert RT.recover_confidence_original(
        {"confidence": 0.95, "reason": "[ark] 无法判断", "evidence": ""}) == 0.95
    print("recover_confidence_original: PASS")


def test_review_kind_for_reason():
    assert RT.review_kind_for_reason("unreadable:empty") == "unreadable"
    assert RT.review_kind_for_reason("no-text:待确认") == "no_text"
    assert RT.review_kind_for_reason("scanned-no-text") == "no_text"
    assert RT.review_kind_for_reason("unsupported_ext:.xyz") == "unsupported"
    assert RT.review_kind_for_reason("unsupported_codec:.wmv") == "unsupported"
    assert RT.review_kind_for_reason("blurry:12.5") == "blur"
    assert RT.review_kind_for_reason("llm:0.70") == "low_conf"
    assert RT.review_kind_for_reason("low:margin:0.04") == "low_conf"
    assert RT.review_kind_for_reason("", "ambiguous") == "ambiguous"
    print("review_kind_for_reason: PASS")


# ---------------------------------------------------------------- §3.7 EXIF

def _make_jpeg_with_exif(path, date_str):
    from PIL import Image
    import numpy as np
    arr = np.random.default_rng(1).integers(0, 256, (200, 200, 3), dtype="uint8")
    im = Image.fromarray(arr)
    exif = Image.Exif()
    exif[36867] = date_str
    im.save(path, exif=exif)


def _make_mp4_with_mvhd(path, year, month):
    base = datetime.datetime(1904, 1, 1, tzinfo=datetime.timezone.utc)
    target = datetime.datetime(year, month, 2, 12, 0, 0, tzinfo=datetime.timezone.utc)
    ts = int((target - base).total_seconds())
    mvhd_body = struct.pack(">I", 0) + struct.pack(">I", ts) + struct.pack(">I", ts) \
        + struct.pack(">I", 1000) + struct.pack(">I", 0) + b"\x00" * 40
    mvhd = struct.pack(">I", 8 + len(mvhd_body)) + b"mvhd" + mvhd_body
    ftyp = struct.pack(">I", 24) + b"ftyp" + b"isom" + struct.pack(">I", 0) + b"isomiso2"
    with open(path, "wb") as f:
        f.write(ftyp + mvhd + b"\x00" * 100)


def test_exif_datetime():
    from common.tags import exif_datetime
    with tempfile.TemporaryDirectory() as tmp:
        jpg = os.path.join(tmp, "a.jpg")
        _make_jpeg_with_exif(jpg, "2024:03:15 10:20:30")
        assert exif_datetime(jpg) == (2024, 3)
        # 无 EXIF -> None；坏年份（相机时钟错）-> None。
        from PIL import Image
        import numpy as np
        arr = np.random.default_rng(2).integers(0, 256, (50, 50, 3), dtype="uint8")
        plain = os.path.join(tmp, "b.jpg")
        Image.fromarray(arr).save(plain)
        assert exif_datetime(plain) is None
        bad = os.path.join(tmp, "c.jpg")
        exif = Image.Exif()
        exif[36867] = "1999:01:01 00:00:00"
        Image.fromarray(arr).save(bad, exif=exif)
        assert exif_datetime(bad) is None
        # mp4 mvhd creation_time。
        mp4 = os.path.join(tmp, "v.mp4")
        _make_mp4_with_mvhd(mp4, 2023, 11)
        assert exif_datetime(mp4) == (2023, 11)
        mp4b = os.path.join(tmp, "v2.mp4")
        with open(mp4b, "wb") as f:
            f.write(b"\x00" * 100)
        assert exif_datetime(mp4b) is None
        # 非媒体扩展名 -> None。
        assert exif_datetime(os.path.join(tmp, "x.txt")) is None
    print("exif_datetime: PASS")


# ---------------------------------------------------------------- 媒体引擎

def _media_cfg(tmp):
    return {
        "library_root": os.path.join(tmp, "lib"),
        "inbox": os.path.join(tmp, "inbox"),
        "review_root": os.path.join(tmp, "lib", "_Review"),
        "work_dir": os.path.join(tmp, "work"),
        "model_dir": os.path.join(tmp, "models"),
        "settle_minutes": 0,
        "max_files_per_run": 300,
        "thresholds": {"margin_min": 0.12, "score_min": 0.35, "blur_min": 50,
                       "phash_max_distance": 5, "near_dup_enabled": True},
        "photo_dirs": {"儿童成长": "照片/家庭与人物/儿童成长",
                       "日常生活": "照片/生活记录/日常生活",
                       "旅行风景": "照片/旅行与城市/旅行风景"},
        "video_dirs": {"儿童成长": "视频/儿童成长",
                       "日常生活": "视频/日常生活"},
        "review_dirs": {"待确认": "_Review/待确认", "模糊": "_Review/模糊",
                        "疑似重复": "_Review/疑似重复"},
    }


class _StubClassifier:
    """假分类器：scores() 返回指定 top1/top2。"""

    def __init__(self, top1="儿童成长", s1=0.80, top2="日常生活", s2=0.01):
        self.top1, self.s1, self.top2, self.s2 = top1, s1, top2, s2

    def scores(self, pil, kind):
        sig = {self.top1: self.s1, self.top2: self.s2,
               "屏幕截图": 0.0, "文档与票据": 0.0}
        return {"sig": dict(sig), "zh": dict(sig),
                "sig_top": (self.top1, self.s1), "sig_2": (self.top2, self.s2)}


def test_media_review_item_keeps_suggested_dst():
    """classifier=None 时 no_classifier 复核项保留建议目标（不再清空）。"""
    pytest.importorskip("PIL")
    pytest.importorskip("cv2")
    from media_core.engine import plan_media_files
    import numpy as np
    from PIL import Image
    with tempfile.TemporaryDirectory() as tmp:
        arr = np.random.default_rng(3).integers(0, 256, (200, 200, 3), dtype="uint8")
        p = os.path.join(tmp, "n.png")
        Image.fromarray(arr).save(p)
        items, stats = plan_media_files([p], _media_cfg(tmp), classifier=None,
                                        index=None)
        assert len(items) == 1
        it = items[0]
        assert it["action"] == "review" and it["review_kind"] == "ambiguous"
        # 0.2.5 §3.2：复核项保留建议目标 dst（不再清空）。
        assert it["dst"] != "", it
        assert it["dst"].endswith(os.path.join("_Review", "待确认", "n.png")), it["dst"]
        assert it["confidence_original"] == it["confidence"] == 0.0
    print("media review keeps suggested_dst: PASS")


def test_media_exif_accepted_placement():
    """接受分支 + EXIF：按年/月归档，内容分类只进 tags。"""
    pytest.importorskip("PIL")
    pytest.importorskip("cv2")
    from media_core.engine import plan_media_files
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "kid.jpg")
        _make_jpeg_with_exif(p, "2024:03:15 10:20:30")
        items, stats = plan_media_files(
            [p], _media_cfg(tmp),
            classifier=_StubClassifier("儿童成长", 0.80, "日常生活", 0.01),
            index=None)
        assert len(items) == 1, items
        it = items[0]
        assert it["action"] == "move", it
        assert it["exif_placed"] is True
        assert it["reason"].startswith("exif:2024-03"), it["reason"]
        assert it["dst"].endswith(os.path.join("照片", "2024", "2024-03", "kid.jpg")), it["dst"]
        assert it["category"] == "儿童成长"
        assert "儿童成长" in it["tags"] and "照片" in it["tags"]
        assert it["confidence_original"] == it["confidence"] == 0.80
    print("media exif accepted placement: PASS")


def test_media_exif_low_conf_placement():
    """低置信 + EXIF：仍按年/月定位置放（归位不依赖内容判断）。"""
    pytest.importorskip("PIL")
    pytest.importorskip("cv2")
    from media_core.engine import plan_media_files
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "kid2.jpg")
        _make_jpeg_with_exif(p, "2023:11:02 08:00:00")
        items, stats = plan_media_files(
            [p], _media_cfg(tmp),
            classifier=_StubClassifier("儿童成长", 0.40, "日常生活", 0.35),
            index=None)
        assert len(items) == 1, items
        it = items[0]
        assert it["action"] == "move", it
        assert it["exif_placed"] is True
        assert it["dst"].endswith(os.path.join("照片", "2023", "2023-11", "kid2.jpg")), it["dst"]
    print("media exif low_conf placement: PASS")


def test_media_low_conf_no_exif_review_keeps_dst():
    """低置信 + 无 EXIF：进复核，但保留类目建议目标与原始置信度。"""
    pytest.importorskip("PIL")
    pytest.importorskip("cv2")
    from media_core.engine import plan_media_files
    import numpy as np
    from PIL import Image
    with tempfile.TemporaryDirectory() as tmp:
        arr = np.random.default_rng(4).integers(0, 256, (200, 200, 3), dtype="uint8")
        p = os.path.join(tmp, "n2.png")
        Image.fromarray(arr).save(p)
        items, stats = plan_media_files(
            [p], _media_cfg(tmp),
            classifier=_StubClassifier("日常生活", 0.40, "儿童成长", 0.35),
            index=None)
        assert len(items) == 1, items
        it = items[0]
        assert it["action"] == "review" and it["review_kind"] == "low_conf", it
        assert it["dst"].endswith(os.path.join(
            "照片", "生活记录", "日常生活", "n2.png")), it["dst"]
        assert it["confidence"] == 0.40 or it["confidence"] == pytest.approx(0.40)
        assert it["confidence_original"] == pytest.approx(0.40)
        assert "日常生活" in it["tags"]
    print("media low_conf review keeps dst: PASS")


# ---------------------------------------------------------------- 文档引擎

def test_doc_plan_item_keeps_dst_and_original_conf():
    """文档 review 行保留 dst 作建议，confidence_original 为压之前的值。"""
    from curator_core import engine as CE
    row = {"src": "/vol1/1000/收件箱/x.pdf", "category": "04-供应商与采购",
           "ext": ".pdf", "reason": "default", "confidence": 0.70,
           "score": 0, "target_name": "x.pdf"}
    item = CE._to_plan_item(row, "/vol1/1000/工作文件", 8, False, {}, {})
    assert item["action"] == "review" and item["review_kind"] == "low_conf"
    assert item["dst"] == "/vol1/1000/工作文件/04-供应商与采购/x.pdf", item["dst"]
    assert item["confidence"] == 0.45
    assert item["confidence_original"] == 0.70
    print("doc plan item keeps dst: PASS")


# ---------------------------------------------------------------- 复核 API

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
    from common import db as DB
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
    from common import db as DB
    conn = DB.connect(c._db)
    rid = DB.add_review(conn, plan_id=c._plan, kind=kind, payload=payload)
    conn.close()
    return rid


def _make_src(c, rel, content=b"x"):
    p = c._tmp / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return str(p)


def test_bulk_approve_moves_file(client):
    """§3.2：批量批准真的搬文件（此前只改状态）。"""
    src = _make_src(client, "收件箱/a.pdf", b"pdf-bytes")
    dst = str(client._tmp / "工作文件" / "04-供应商与采购" / "a.pdf")
    rid = _add_review(client, "low_conf", {
        "src": src, "category": "04-供应商与采购", "confidence": 0.70,
        "confidence_original": 0.70, "reason": "llm:0.70", "evidence": "",
        "suggested_dst": dst, "tags": []})
    r = client.post("/api/review/bulk", json={"ids": [rid], "decision": "approved"})
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["ok"] and res["moved"] is True, res
    assert os.path.exists(dst) and not os.path.exists(src)
    print("bulk approve moves file: PASS")


def test_bulk_approve_without_dst_only_updates_status(client):
    """无建议目标时批量批准仍只改状态（不报错、不丢文件）。"""
    src = _make_src(client, "收件箱/b.pdf", b"pdf-bytes")
    rid = _add_review(client, "low_conf", {
        "src": src, "category": "待确认", "confidence": 0.45,
        "reason": "llm:0.70", "evidence": "", "suggested_dst": "", "tags": []})
    r = client.post("/api/review/bulk", json={"ids": [rid], "decision": "approved"})
    res = r.json()["results"][0]
    assert res["ok"] and res["moved"] is False
    assert os.path.exists(src)
    print("bulk approve without dst: PASS")


def test_reclassify_dry_run_and_apply(client):
    """§4.3-4：重分类 dry-run 报告可自动数，apply=true 真的移动。"""
    # llm 0.80：新阈值 0.75 下可自动。
    src1 = _make_src(client, "收件箱/c.pdf", b"c")
    rid1 = _add_review(client, "low_conf", {
        "src": src1, "category": "04-供应商与采购", "confidence": 0.45,
        "reason": "llm:0.80;path-prior:低置信0.80不跨顶层", "evidence": "",
        "suggested_dst": "", "tags": []})
    # llm 0.70：未达 0.75，不可自动。
    src2 = _make_src(client, "收件箱/d.pdf", b"d")
    rid2 = _add_review(client, "low_conf", {
        "src": src2, "category": "04-供应商与采购", "confidence": 0.45,
        "reason": "llm:0.70", "evidence": "", "suggested_dst": "", "tags": []})
    # 无有效分类：跳过。
    src3 = _make_src(client, "收件箱/e.pdf", b"e")
    rid3 = _add_review(client, "low_conf", {
        "src": src3, "category": "待确认", "confidence": 0.95,
        "reason": "[ark] 无法判断", "evidence": "", "suggested_dst": "", "tags": []})
    # dup：跳过。
    src4 = _make_src(client, "收件箱/f.pdf", b"f")
    rid4 = _add_review(client, "dup", {
        "src": src4, "category": "疑似重复", "confidence": 0.9,
        "reason": "dup:sha256", "evidence": "", "suggested_dst": "", "tags": []})
    r = client.post("/api/review/reclassify", json={})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 4 and d["eligible"] == 1, d
    assert d["not_eligible"] == 1 and d["skipped"] == 2, d
    assert d["applied"] == 0 and d["moved"] == 0
    assert os.path.exists(src1)  # dry-run 不动文件
    # apply=true：可自动的行真的移动。
    r = client.post("/api/review/reclassify", json={"apply": True})
    d = r.json()
    assert d["eligible"] == 1 and d["moved"] == 1, d
    dst1 = str(client._tmp / "工作文件" / "04-供应商与采购" / "c.pdf")
    assert os.path.exists(dst1) and not os.path.exists(src1)
    assert os.path.exists(src2)  # 未达阈值的行不动
    print("reclassify dry-run + apply: PASS")


def test_review_pagination_and_hide_misc(client):
    """§5.1 分页 + §3.4 杂项默认过滤。"""
    for i in range(3):
        _add_review(client, "low_conf", {
            "src": "/vol1/1000/收件箱/f%d.pdf" % i, "category": "04-供应商与采购",
            "confidence": 0.7, "reason": "llm:0.70", "evidence": "",
            "suggested_dst": "", "tags": []})
    _add_review(client, "unreadable", {
        "src": "/vol1/1000/收件箱/bad.pdf", "category": "待确认",
        "confidence": 0.0, "reason": "unreadable:empty", "evidence": "",
        "suggested_dst": "", "tags": []})
    r = client.get("/api/review?status=pending&limit=2&offset=0")
    d = r.json()
    assert d["total"] == 4 and len(d["items"]) == 2 and d["limit"] == 2
    r = client.get("/api/review?status=pending&limit=2&offset=2")
    assert len(r.json()["items"]) == 2
    # hide_misc：杂项被过滤。
    r = client.get("/api/review?status=pending&hide_misc=1")
    kinds = {x["kind"] for x in r.json()["items"]}
    assert "unreadable" not in kinds and r.json()["total"] == 3
    r = client.get("/api/review?status=pending")
    assert r.json()["total"] == 4
    # folders 分页信封。
    r = client.get("/api/review/folders?status=pending&limit=1&offset=0")
    d = r.json()
    assert "groups" in d and "total" in d and "total_items" in d
    assert d["total_items"] == 4
    print("pagination + hide_misc: PASS")


def test_review_suggest_level(client):
    """§3.3：原始置信度 >= review_suggest 标建议批准，否则需人工判断。"""
    _add_review(client, "low_conf", {
        "src": "/vol1/1000/收件箱/g.pdf", "category": "04-供应商与采购",
        "confidence": 0.45, "confidence_original": 0.80,
        "reason": "llm:0.80", "evidence": "", "suggested_dst": "", "tags": []})
    _add_review(client, "low_conf", {
        "src": "/vol1/1000/收件箱/h.pdf", "category": "04-供应商与采购",
        "confidence": 0.40, "reason": "llm:0.40", "evidence": "",
        "suggested_dst": "", "tags": []})
    r = client.get("/api/review?status=pending")
    items = {x["payload"]["src"]: x for x in r.json()["items"]}
    assert items["/vol1/1000/收件箱/g.pdf"]["suggest_level"] == "suggest"
    assert items["/vol1/1000/收件箱/g.pdf"]["confidence_original"] == 0.80
    assert items["/vol1/1000/收件箱/h.pdf"]["suggest_level"] == "manual"
    print("suggest_level: PASS")


def test_dup_dry_run(client):
    """§3.6：dup dry-run 只报数，不移动、不删除。"""
    src = _make_src(client, "收件箱/dup.pdf", b"dup")
    _add_review(client, "dup", {
        "src": src, "category": "疑似重复", "confidence": 0.9,
        "reason": "dup:sha256", "evidence": "", "suggested_dst": "", "tags": []})
    _add_review(client, "dup", {
        "src": str(client._tmp / "收件箱" / "gone.pdf"), "category": "疑似重复",
        "confidence": 0.9, "reason": "dup:sha256", "evidence": "",
        "suggested_dst": "", "tags": []})
    r = client.get("/api/review/dup/dry-run")
    d = r.json()
    assert d["total"] == 2 and d["src_exists"] == 1 and d["src_gone"] == 1, d
    assert os.path.exists(src)  # dry-run 不动文件
    assert "06-重复待确认" in d["sample"][0]["holding_dst"]
    print("dup dry-run: PASS")


def test_version_consistent_everywhere():
    """0.2.5 教训：升版须四处一致（VERSION、根 compose、fpk compose、
    Dockerfile LABEL）——0.2.5 首轮升级就因 fpk compose 仍写 0.2.4 被
    升级脚本的镜像校验拦下（status.json：新 compose 里镜像不是 0.2.5）。
    0.2.6：再加 fpk/manifest 的 version，共五处一致。"""
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    v = json.load(open(os.path.join(root, "VERSION"), encoding="utf-8"))["version"]
    for rel in ("docker-compose.yml", "fpk/app/docker/docker-compose.yaml"):
        text = open(os.path.join(root, rel), encoding="utf-8").read()
        assert "image: nas-curator:%s" % v in text, (rel, v)
    dockerfile = open(os.path.join(root, "Dockerfile"), encoding="utf-8").read()
    assert 'org.opencontainers.image.version="%s"' % v in dockerfile
    manifest = open(os.path.join(root, "fpk", "manifest"), encoding="utf-8").read()
    assert re.search(r"^version\s*=\s*%s\s*$" % re.escape(v), manifest, re.M), v
    print("version consistent: %s" % v)


def test_review_kind_migration_old_db(tmp_path):
    """§3.4：旧库（CHECK 不含新 kind）经 init_db 迁移后可写入新 kind，旧行不丢。"""
    import sqlite3
    from common import db as DB
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE review_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER, plan_id INTEGER,
            kind TEXT NOT NULL CHECK (kind IN ('low_conf', 'dup', 'blur',
                'ambiguous', 'shell', 'sensitive')),
            payload TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            decided_by TEXT, decided_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
        )""")
    conn.execute("INSERT INTO review_queue (kind, payload) VALUES ('low_conf', '{}')")
    conn.commit()
    conn.close()
    DB.init_db(db)
    conn = DB.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0]
    assert n == 1, "迁移不应丢行"
    for k in ("unreadable", "unsupported", "no_text"):
        DB.add_review(conn, kind=k, payload={"src": "/tmp/%s.pdf" % k})
    n = conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0]
    assert n == 4
    conn.close()
    # 幂等：再跑一次 init_db 仍正常。
    DB.init_db(db)
    conn = DB.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 4
    conn.close()
    print("review kind migration: PASS")
