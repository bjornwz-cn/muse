#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0.1.2：路径强信号 / 人优先 / 复核裁定语义（dup holding、下拉改顶层、current_top）。

只用临时目录与临时 DB，不碰项目 data/nas-curator.db 与 NAS。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from common import routing as RT

ROOTS = {
    "work": "/vol1/1000/工作文件",
    "family": "/vol1/1000/家庭文件",
    "media": "/vol1/1000/媒体库",
    "inbox": "/vol1/1000/收件箱",
    "mac-backup": "/vol1/1000/下载/Wangzhen-Mac",
}


def test_library_top_and_labels():
    assert RT.library_top("/vol1/1000/家庭文件/刘音/会议/x.jpg", ROOTS) == "family"
    assert RT.library_top("/vol1/1000/工作文件/归档/x.pdf", ROOTS) == "work"
    assert RT.library_top("/vol1/1000/媒体库/照片/x.jpg", ROOTS) == "media"
    assert RT.library_top("/vol1/1000/收件箱/x.txt", ROOTS) == "inbox"
    assert RT.library_top("/vol1/1000/下载/Wangzhen-Mac/下载/x.txt", ROOTS) == "mac-backup"
    assert RT.library_top("/vol1/1000/别处/x.txt", ROOTS) is None
    assert RT.top_label("family") == "家庭"
    assert RT.top_label("mac-backup") == "笔记本备份"
    assert RT.top_label(None) == "其他"
    assert RT.family_person_of("/vol1/1000/家庭文件/刘音/会议/x.jpg") == "刘音"
    assert RT.family_person_of("/vol1/1000/工作文件/x.pdf") == ""
    assert RT.path_prior_label("/vol1/1000/家庭文件/王震/通话录音/x.m4a", ROOTS) == "家庭/王震/通话录音"
    assert RT.path_prior_label("/vol1/1000/下载/Wangzhen-Mac/x.txt", ROOTS) is None
    print("library_top/labels: PASS")


def test_family_work_dst():
    dst = RT.rewrite_family_work_dst("/vol1/1000/家庭文件", "刘音",
                                     "2015-11-28 094806.jpg", "09-会议与汇报")
    assert dst == "/vol1/1000/家庭文件/刘音/工作/会议/2015-11-28 094806.jpg", dst
    dst = RT.rewrite_family_work_dst("/vol1/1000/家庭文件", "刘音",
                                     "发票-2026.jpg", "07-财务与经营分析")
    assert dst == "/vol1/1000/家庭文件/刘音/工作/票据/发票-2026.jpg", dst
    assert RT.is_work_category("09-会议与汇报") is True
    assert RT.is_work_category("06-其他") is True
    assert RT.is_work_category("17-教育与成长") is False
    print("family_work_dst: PASS")


def test_dup_holding_dst():
    assert RT.dup_holding_dst("/vol1/1000/家庭文件/刘音/会议/x.jpg", ROOTS) == \
        "/vol1/1000/家庭文件/刘音/99-待确认/06-重复待确认/x.jpg"
    assert RT.dup_holding_dst("/vol1/1000/工作文件/归档/x.pdf", ROOTS) == \
        "/vol1/1000/工作文件/99-待确认/06-重复待确认/x.pdf"
    assert RT.dup_holding_dst("/vol1/1000/下载/Wangzhen-Mac/下载/x.png", ROOTS) == \
        "/vol1/1000/下载/Wangzhen-Mac/99-待确认/06-重复待确认/x.png"
    print("dup_holding_dst: PASS")


def test_path_prior_guard():
    # 低置信跨顶层 -> 转 review
    items = [{"src": "/vol1/1000/家庭文件/刘音/会议/x.jpg",
              "dst": "/vol1/1000/工作文件/09-会议与汇报/x.jpg",
              "action": "move", "category": "09-会议与汇报",
              "confidence": 0.7, "reason": "rule", "review_kind": None,
              "auto_exempt": False}]
    out, info = RT.apply_path_prior_guard(items, ROOTS, 0.85)
    assert info["guarded"] == 1
    assert out[0]["action"] == "review" and out[0]["dst"] == ""
    assert out[0]["review_kind"] == "low_conf"
    assert out[0]["confidence"] == 0.45
    assert "path-prior" in out[0]["reason"]
    # 高置信跨顶层 -> 保留 move
    items = [{"src": "/vol1/1000/家庭文件/刘音/会议/x.jpg",
              "dst": "/vol1/1000/工作文件/09-会议与汇报/x.jpg",
              "action": "move", "category": "09-会议与汇报",
              "confidence": 0.9, "reason": "rule", "review_kind": None,
              "auto_exempt": False}]
    out, info = RT.apply_path_prior_guard(items, ROOTS, 0.85)
    assert info["guarded"] == 0 and out[0]["action"] == "move"
    # 同顶层 -> 保留
    items = [{"src": "/vol1/1000/家庭文件/刘音/会议/x.jpg",
              "dst": "/vol1/1000/家庭文件/刘音/工作/会议/x.jpg",
              "action": "move", "category": "09-会议与汇报",
              "confidence": 0.4, "reason": "rule", "review_kind": None,
              "auto_exempt": False}]
    out, info = RT.apply_path_prior_guard(items, ROOTS, 0.85)
    assert info["guarded"] == 0 and out[0]["action"] == "move"
    # unit 整目录：2026-11-24 计划 #160 修复后不再豁免——低置信跨顶层照样转复核
    # （本例即 #160 原形：unit/project、置信度为空即 0、跨顶层到工作文件）
    items = [{"src": "/vol1/1000/下载/Wangzhen-Mac/项目",
              "dst": "/vol1/1000/工作文件/12-项目资料/项目",
              "action": "move", "category": "12-项目资料/项目",
              "confidence": "", "reason": "unit/project", "review_kind": None,
              "auto_exempt": True}]
    out, info = RT.apply_path_prior_guard(items, ROOTS, 0.85)
    assert info["guarded"] == 1 and out[0]["action"] == "review"
    assert out[0]["review_kind"] == "low_conf" and out[0]["dst"] == ""
    # 高置信 unit 跨顶层：路径先验不拦（置信度够），但自动执行门控会拦（见
    # test_apply_gating），此处只确认路径先验本身不因 unit 身份放行低置信项
    items = [{"src": "/vol1/1000/下载/Wangzhen-Mac/项目",
              "dst": "/vol1/1000/工作文件/12-项目资料/项目",
              "action": "move", "category": "12-项目资料/项目",
              "confidence": 0.9, "reason": "unit/project", "review_kind": None,
              "auto_exempt": True}]
    out, info = RT.apply_path_prior_guard(items, ROOTS, 0.85)
    assert info["guarded"] == 0 and out[0]["action"] == "move"
    print("path_prior_guard: PASS")


def test_is_auto_apply_eligible():
    """2026-11-24 计划 #160 回归：唯一自动执行判定入口。"""
    E = RT.is_auto_apply_eligible
    # 0.9 置信度普通文件：可自动执行
    assert E({"action": "move", "kind": "document", "ext": ".txt",
              "confidence": 0.9}, 0.85) is True
    assert E({"action": "rename", "kind": "media", "ext": ".jpg",
              "confidence": 0.85}, 0.85) is True
    # 0.4 置信度普通文件：不可自动执行
    assert E({"action": "move", "kind": "media", "ext": ".jpg",
              "confidence": 0.4}, 0.85) is False
    # 0.4 置信度 unit 整目录（#160 原形）：不可自动执行
    assert E({"action": "move", "kind": "unit", "ext": "(目录)",
              "confidence": 0.4, "auto_exempt": True}, 0.85) is False
    # 0.9 置信度 unit 整目录：仍不可自动执行（unit 永不自动执行）
    assert E({"action": "move", "kind": "unit", "ext": "(目录)",
              "confidence": 0.9, "auto_exempt": True}, 0.85) is False
    # auto_exempt 不再赋予资格：0.4 置信度文件即使标 auto_exempt 也不行
    assert E({"action": "move", "kind": "document", "ext": ".txt",
              "confidence": 0.4, "auto_exempt": True}, 0.85) is False
    # 非 move/rename 动作：不可自动执行
    assert E({"action": "review", "kind": "document", "ext": ".txt",
              "confidence": 0.95}, 0.85) is False
    print("is_auto_apply_eligible: PASS")


def test_route_family_items_path_signal_and_work_subdir():
    import curator as CUR
    cfg = {"libraries": [
        {"id": "work", "root": "/vol1/1000/工作文件"},
        {"id": "family", "root": "/vol1/1000/家庭文件"},
        {"id": "media", "root": "/vol1/1000/媒体库"},
    ]}
    # 文件名无人名，但源路径含刘音 -> 家庭分流，且工作内容进 工作/会议/
    items = [{"src": "/vol1/1000/下载/Wangzhen-Mac/刘音/会议/2015-11-28 094806.jpg",
              "dst": "/vol1/1000/工作文件/09-会议与汇报/2015-11-28 094806.jpg",
              "action": "move", "kind": "document",
              "category": "09-会议与汇报", "evidence": "", "tags": []}]
    out = CUR._route_family_items(items, cfg)
    assert out[0]["library"] == "family", out[0]
    assert out[0]["dst"] == \
        "/vol1/1000/家庭文件/刘音/工作/会议/2015-11-28 094806.jpg", out[0]["dst"]
    # 家庭个人内容（17-教育与成长）不加「工作」层
    items = [{"src": "/vol1/1000/下载/Wangzhen-Mac/王子轩/作业.pdf",
              "dst": "/vol1/1000/工作文件/17-教育与成长/作业.pdf",
              "action": "move", "kind": "document",
              "category": "17-教育与成长", "evidence": "", "tags": []}]
    out = CUR._route_family_items(items, cfg)
    assert out[0]["dst"] == "/vol1/1000/家庭文件/王子轩/17-教育与成长/作业.pdf", out[0]["dst"]
    print("route_family_items: PASS")


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
        "thresholds": {"auto": 0.85, "review_suggest": 0.6}}
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


def test_review_list_current_top(client):
    rid = _add_review(client, "low_conf", {
        "src": "/vol1/1000/家庭文件/刘音/会议/2015-11-28 094806 (1).jpg",
        "category": "99-待确认/04-低置信", "confidence": 0.4,
        "reason": "no-text:待确认", "evidence": "", "suggested_dst": "", "tags": []})
    r = client.get("/api/review?status=pending")
    assert r.status_code == 200, r.text
    rows = [x for x in r.json() if x["id"] == rid]
    assert len(rows) == 1
    # 注意：fixture 的库根是 tmp 路径，线上 /vol1 路径认不出顶层；
    # 这里只断言字段存在且为字符串（真实映射由 test_library_top 覆盖）。
    assert isinstance(rows[0]["current_top"], str)
    assert "current_top_id" in rows[0]
    print("review list current_top: PASS")


def test_review_list_current_top_tmp_roots(client):
    # 用 fixture 的 tmp 库根建一条复核，current_top 应为 家庭
    fam_dir = client._tmp / "家庭文件" / "刘音" / "会议"
    fam_dir.mkdir(parents=True)
    src = fam_dir / "x.jpg"
    src.write_bytes(b"fake-jpg")
    rid = _add_review(client, "low_conf", {
        "src": str(src), "category": "99-待确认/04-低置信", "confidence": 0.4,
        "reason": "no-text:待确认", "evidence": "", "suggested_dst": "", "tags": []})
    r = client.get("/api/review?status=pending")
    rows = [x for x in r.json() if x["id"] == rid]
    assert rows[0]["current_top"] == "家庭", rows[0]
    assert rows[0]["current_top_id"] == "family"
    print("review list current_top(tmp): PASS")


def test_dup_approve_moves_to_holding_not_delete(client):
    fam_dir = client._tmp / "家庭文件" / "刘音" / "会议"
    fam_dir.mkdir(parents=True)
    src = fam_dir / "2015-11-28 094806.jpg"
    src.write_bytes(b"fake-jpg-bytes")
    rid = _add_review(client, "dup", {
        "src": str(src), "category": "99-待确认/06-重复待确认", "confidence": 0.45,
        "reason": "dup:sha256",
        "evidence": str(fam_dir / "2015-11-28 094806 (1).jpg"),
        "suggested_dst": "", "tags": []})
    r = client.post(f"/api/review/{rid}/decide", json={"decision": "approved"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["moved"] is True, d
    holding = client._tmp / "家庭文件" / "刘音" / "99-待确认" / "06-重复待确认" / "2015-11-28 094806.jpg"
    assert d["dst"] == str(holding), d
    assert holding.exists(), "holding 区应有文件（未删除）"
    assert not src.exists(), "源文件应已移走"
    assert holding.read_bytes() == b"fake-jpg-bytes"
    print("dup approve -> holding: PASS")


def test_dup_reject_keeps_file(client):
    fam_dir = client._tmp / "家庭文件" / "刘音" / "会议"
    fam_dir.mkdir(parents=True)
    src = fam_dir / "y.jpg"
    src.write_bytes(b"y")
    rid = _add_review(client, "dup", {
        "src": str(src), "category": "99-待确认/06-重复待确认", "confidence": 0.45,
        "reason": "dup:sha256", "evidence": "", "suggested_dst": "", "tags": []})
    r = client.post(f"/api/review/{rid}/decide", json={"decision": "rejected"})
    assert r.status_code == 200, r.text
    assert r.json()["moved"] is False
    assert src.exists(), "拒绝后文件应原地不动"
    print("dup reject keeps file: PASS")


def test_low_conf_approve_no_dst_status_only(client):
    fam_dir = client._tmp / "家庭文件" / "刘音" / "会议"
    fam_dir.mkdir(parents=True)
    src = fam_dir / "z.jpg"
    src.write_bytes(b"z")
    rid = _add_review(client, "low_conf", {
        "src": str(src), "category": "99-待确认/04-低置信", "confidence": 0.4,
        "reason": "no-text:待确认", "evidence": "", "suggested_dst": "", "tags": []})
    # 下拉未改（家庭 == 当前顶层）-> 仅更新状态，不移动
    r = client.post(f"/api/review/{rid}/decide",
                    json={"decision": "approved", "category": "家庭"})
    assert r.status_code == 200, r.text
    assert r.json()["moved"] is False, r.json()
    assert src.exists()
    print("low_conf approve no-dst status-only: PASS")


def test_top_change_recomputes_dst(client):
    fam_dir = client._tmp / "家庭文件" / "刘音" / "会议"
    fam_dir.mkdir(parents=True)
    src = fam_dir / "w.jpg"
    src.write_bytes(b"w")
    suggested = str(fam_dir / "w.jpg")
    rid = _add_review(client, "low_conf", {
        "src": str(src), "category": "09-会议与汇报", "confidence": 0.7,
        "reason": "low:margin", "evidence": "",
        "suggested_dst": suggested, "tags": []})
    # 下拉改成「工作」-> dst 只换库根：工作文件/刘音/会议/w.jpg
    r = client.post(f"/api/review/{rid}/decide",
                    json={"decision": "approved", "category": "工作"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["moved"] is True, d
    expect = client._tmp / "工作文件" / "刘音" / "会议" / "w.jpg"
    assert d["dst"] == str(expect), d
    assert expect.exists() and not src.exists()
    print("top change recomputes dst: PASS")


def test_reevaluate_decide_row(tmp_path):
    import importlib.util
    proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "reevaluate_review", os.path.join(proj, "scripts", "reevaluate_review.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    roots = {
        "work": str(tmp_path / "工作文件"),
        "family": str(tmp_path / "家庭文件"),
        "media": str(tmp_path / "媒体库"),
        "inbox": str(tmp_path / "收件箱"),
        "mac-backup": str(tmp_path / "下载" / "Wangzhen-Mac"),
    }
    fam = tmp_path / "家庭文件" / "王震" / "通话录音"
    fam.mkdir(parents=True)
    f = fam / "x.m4a"
    f.write_bytes(b"0")
    # default 原因 + 已在正确位置 -> clear
    action, label, _ = mod.decide_row(
        {"src": str(f), "category": "06-其他", "confidence": 0.4,
         "reason": "default"}, roots)
    assert action == "clear" and label == "家庭/王震/通话录音"
    # no-text + 已在正确位置 -> clear
    action, label, _ = mod.decide_row(
        {"src": str(f), "category": "99-待确认/04-低置信", "confidence": 0.4,
         "reason": "no-text:待确认"}, roots)
    assert action == "clear"
    # 真实分类器低置信结论 -> downgrade（保留在队列，改路径标签）
    action, label, _ = mod.decide_row(
        {"src": str(f), "category": "07-财务与经营分析", "confidence": 0.45,
         "reason": "low:margin:0.08"}, roots)
    assert action == "downgrade" and label == "家庭/王震/通话录音"
    # 收件箱文件（未归位）-> keep
    action, label, _ = mod.decide_row(
        {"src": str(tmp_path / "收件箱" / "x.txt"), "category": "06-其他",
         "confidence": 0.4, "reason": "default"}, roots)
    assert action == "keep" and label is None
    # 源文件已不在 -> keep
    action, label, _ = mod.decide_row(
        {"src": str(fam / "gone.m4a"), "category": "06-其他",
         "confidence": 0.4, "reason": "default"}, roots)
    assert action == "keep"
    print("reevaluate decide_row: PASS")


def test_top_change_no_suggested_dst_uses_default_first_level(client):
    fam_dir = client._tmp / "家庭文件" / "刘音" / "会议"
    fam_dir.mkdir(parents=True)
    src = fam_dir / "v.jpg"
    src.write_bytes(b"v")
    rid = _add_review(client, "low_conf", {
        "src": str(src), "category": "99-待确认/04-低置信", "confidence": 0.4,
        "reason": "no-text:待确认", "evidence": "", "suggested_dst": "", "tags": []})
    # 无 suggested_dst，下拉改成「工作」-> 默认落 工作文件/归档/
    r = client.post(f"/api/review/{rid}/decide",
                    json={"decision": "approved", "category": "工作"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["moved"] is True, d
    expect = client._tmp / "工作文件" / "归档" / "v.jpg"
    assert d["dst"] == str(expect), d
    assert expect.exists() and not src.exists()
    print("top change no-dst default first level: PASS")
