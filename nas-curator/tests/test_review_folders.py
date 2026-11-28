#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0.1.6：复核队列文件夹级复核（分组展示 + 整包批准/拒绝 + 整包改顶层移动）。

只用临时目录与临时 DB，不碰项目 data/nas-curator.db 与 NAS。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from common import routing as RT

# 注意：api.main 不在模块顶层 import——api/deps.py 在 import 时就把
# CURATOR_CONFIG_DIR 冻结为 CONFIG_DIR；test_wizard.py 依赖在模块 import 前
# 先设该环境变量。顶层 import api.main 会让 wizard 测试写错配置目录
# （P4 回归曾因此失败；0.1.5 曾因此重演）。与 test_api_smoke /
# test_web_flow / test_apply_gating 一致，api 模块只在函数内懒加载。

ROOTS = {
    "work": "/vol1/1000/工作文件",
    "family": "/vol1/1000/家庭文件",
    "media": "/vol1/1000/媒体库",
    "inbox": "/vol1/1000/收件箱",
    "mac-backup": "/vol1/1000/下载/Wangzhen-Mac",
}


# ---------------------------------------------------------------- 文件夹分组键（纯函数）

def test_folder_key_mac_backup_first_level():
    key, top, rel, display = RT.review_folder_for(
        "/vol1/1000/下载/Wangzhen-Mac/项目开发/企业固定资产管理系统/res/a.png",
        ROOTS)
    assert (key, top, rel, display) == (
        "mac-backup:项目开发", "mac-backup", "项目开发", "项目开发"), (key, top, rel, display)
    print("folder key mac-backup first level: PASS")


def test_folder_key_generic_container_descends():
    # 下载是通用容器名，下钻一级取真正的项目文件夹
    key, top, rel, display = RT.review_folder_for(
        "/vol1/1000/下载/Wangzhen-Mac/下载/PaddleOCR-3.7.0/deploy/android_demo/app/src/main/res/mipmap-hdpi/ic_launcher.png",
        ROOTS)
    assert (key, top, rel, display) == (
        "mac-backup:下载/PaddleOCR-3.7.0", "mac-backup",
        "下载/PaddleOCR-3.7.0", "PaddleOCR-3.7.0"), (key, top, rel, display)
    # 直接在 下载/ 下的文件，仍归 下载
    key, top, rel, display = RT.review_folder_for(
        "/vol1/1000/下载/Wangzhen-Mac/下载/x.png", ROOTS)
    assert (key, top, rel, display) == (
        "mac-backup:下载", "mac-backup", "下载", "下载")
    print("folder key generic container descends: PASS")


def test_folder_key_family_person_root_and_unknown():
    key, top, rel, display = RT.review_folder_for(
        "/vol1/1000/家庭文件/刘音/会议/x.jpg", ROOTS)
    assert (key, top, rel, display) == ("family:刘音", "family", "刘音", "刘音")
    # 直接在库根下的文件
    key, top, rel, display = RT.review_folder_for(
        "/vol1/1000/工作文件/x.pdf", ROOTS)
    assert (key, top, rel, display) == ("work:", "work", "", "工作")
    # 不在任何库根下
    key, top, rel, display = RT.review_folder_for("/vol1/1000/别处/x.txt", ROOTS)
    assert (key, top, rel, display) == ("other:", None, "", "其他")
    # 润工作：第一级即文件夹本身
    key, top, rel, display = RT.review_folder_for(
        "/vol1/1000/下载/Wangzhen-Mac/润工作/Y阀投诉资料/x.mp4", ROOTS)
    assert key == "mac-backup:润工作" and display == "润工作"
    print("folder key family/root/unknown: PASS")


# ---------------------------------------------------------------- API（fixture 与 test_review_routing.py 同款）

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


def _seed_grouping(client):
    """3 个文件夹、5 条待复核：项目开发 blur×2 + low_conf×1、
    下载/PaddleOCR-3.7.0 blur×1、家庭刘音 blur×1。路径只作分组依据，
    不建真实文件。"""
    mac = client._tmp / "下载" / "Wangzhen-Mac"
    fam = client._tmp / "家庭文件"
    base = str(mac / "项目开发" / "企业固定资产管理系统" / "res")
    _add_review(client, "blur", {"src": f"{base}/a.png", "category": "06-其他",
                                 "confidence": 0.3, "reason": "blur:26.1",
                                 "evidence": "", "suggested_dst": "", "tags": []})
    _add_review(client, "blur", {"src": f"{base}/b.png", "category": "06-其他",
                                 "confidence": 0.3, "reason": "blur:26.1",
                                 "evidence": "", "suggested_dst": "", "tags": []})
    _add_review(client, "low_conf", {"src": f"{base}/c.png", "category": "06-其他",
                                     "confidence": 0.45, "reason": "low:margin",
                                     "evidence": "", "suggested_dst": "", "tags": []})
    paddle = str(mac / "下载" / "PaddleOCR-3.7.0" / "deploy" / "android_demo")
    _add_review(client, "blur", {"src": f"{paddle}/ic_launcher.png",
                                 "category": "06-其他", "confidence": 0.3,
                                 "reason": "blur:26.1", "evidence": "",
                                 "suggested_dst": "", "tags": []})
    _add_review(client, "blur", {"src": str(fam / "刘音" / "会议" / "x.jpg"),
                                 "category": "06-其他", "confidence": 0.3,
                                 "reason": "blur:26.1", "evidence": "",
                                 "suggested_dst": "", "tags": []})


def test_folders_endpoint_groups_pending(client):
    _seed_grouping(client)
    r = client.get("/api/review/folders", params={"status": "pending"})
    assert r.status_code == 200, r.text
    groups = {g["folder"]: g for g in r.json()["groups"]}
    assert set(groups) == {"mac-backup:项目开发",
                           "mac-backup:下载/PaddleOCR-3.7.0",
                           "family:刘音"}, groups.keys()
    proj = groups["mac-backup:项目开发"]
    assert proj["count"] == 3 and proj["kinds"] == {"blur": 2, "low_conf": 1}, proj
    assert proj["display"] == "项目开发" and proj["top_label"] == "笔记本备份"
    assert len(proj["items"]) == 3
    paddle = groups["mac-backup:下载/PaddleOCR-3.7.0"]
    assert paddle["count"] == 1 and paddle["display"] == "PaddleOCR-3.7.0"
    assert groups["family:刘音"]["count"] == 1
    print("folders endpoint groups pending: PASS")


def test_folders_endpoint_kind_filter(client):
    _seed_grouping(client)
    r = client.get("/api/review/folders",
                   params={"status": "pending", "kind": "blur"})
    groups = {g["folder"]: g for g in r.json()["groups"]}
    assert set(groups) == {"mac-backup:项目开发",
                           "mac-backup:下载/PaddleOCR-3.7.0",
                           "family:刘音"}
    assert groups["mac-backup:项目开发"]["count"] == 2
    assert groups["mac-backup:项目开发"]["kinds"] == {"blur": 2}
    r = client.get("/api/review/folders",
                   params={"status": "pending", "kind": "low_conf"})
    groups = {g["folder"]: g for g in r.json()["groups"]}
    assert set(groups) == {"mac-backup:项目开发"}
    assert groups["mac-backup:项目开发"]["count"] == 1
    print("folders endpoint kind filter: PASS")


def test_folders_endpoint_status_all(client):
    _seed_grouping(client)
    # 先裁定一条（拒绝），status=pending 应少一条，status=all 应全在
    rows = client.get("/api/review", params={"status": "pending"}).json()["items"]
    rid = rows[0]["id"]
    r = client.post(f"/api/review/{rid}/decide", json={"decision": "rejected"})
    assert r.status_code == 200, r.text
    pending = client.get("/api/review/folders",
                         params={"status": "pending"}).json()["groups"]
    assert sum(g["count"] for g in pending) == 4
    all_rows = client.get("/api/review/folders",
                          params={"status": "all"}).json()["groups"]
    assert sum(g["count"] for g in all_rows) == 5
    print("folders endpoint status=all: PASS")


def _make_file(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def test_folder_approve_moves_whole_folder(client):
    mac = client._tmp / "下载" / "Wangzhen-Mac"
    res_dir = mac / "项目开发" / "企业固定资产管理系统" / "res"
    a = _make_file(res_dir / "a.png", b"a")
    b = _make_file(res_dir / "b.png", b"b")
    for src in (a, b):
        _add_review(client, "blur", {"src": src, "category": "06-其他",
                                     "confidence": 0.3, "reason": "blur:26.1",
                                     "evidence": "", "suggested_dst": src,
                                     "tags": []})
    r = client.post("/api/review/folders/approve",
                    json={"folder": "mac-backup:项目开发", "category": "工作"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 2, d
    assert all(x["ok"] and x["moved"] for x in d["results"]), d
    for name, data in (("a.png", b"a"), ("b.png", b"b")):
        dst = client._tmp / "工作文件" / "项目开发" / "企业固定资产管理系统" / "res" / name
        assert dst.exists() and dst.read_bytes() == data, dst
    assert not (res_dir / "a.png").exists() and not (res_dir / "b.png").exists()
    rows = client.get("/api/review", params={"status": "pending"}).json()["items"]
    assert rows == []
    print("folder approve moves whole folder: PASS")


def test_folder_reject_keeps_files(client):
    mac = client._tmp / "下载" / "Wangzhen-Mac"
    res_dir = mac / "项目开发" / "企业固定资产管理系统" / "res"
    a = _make_file(res_dir / "a.png", b"a")
    _add_review(client, "blur", {"src": a, "category": "06-其他",
                                 "confidence": 0.3, "reason": "blur:26.1",
                                 "evidence": "", "suggested_dst": a, "tags": []})
    r = client.post("/api/review/folders/reject",
                    json={"folder": "mac-backup:项目开发"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 1 and d["results"][0]["moved"] is False, d
    assert (res_dir / "a.png").exists(), "拒绝后文件应原地不动"
    print("folder reject keeps files: PASS")


def test_folder_approve_dup_goes_holding(client):
    fam_dir = client._tmp / "家庭文件" / "刘音" / "会议"
    dup_src = _make_file(fam_dir / "x.jpg", b"dup")
    other_src = _make_file(fam_dir / "y.jpg", b"other")
    _add_review(client, "dup", {"src": dup_src, "category": "99-待确认/06-重复待确认",
                                "confidence": 0.45, "reason": "dup:sha256",
                                "evidence": "", "suggested_dst": "", "tags": []})
    _add_review(client, "blur", {"src": other_src, "category": "06-其他",
                                 "confidence": 0.3, "reason": "blur:26.1",
                                 "evidence": "", "suggested_dst": other_src,
                                 "tags": []})
    # 整包改成「工作」：dup 不受改顶层影响，进家庭 holding；其余按单条逻辑移动
    r = client.post("/api/review/folders/approve",
                    json={"folder": "family:刘音", "category": "工作"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 2, d
    holding = client._tmp / "家庭文件" / "刘音" / "99-待确认" / "06-重复待确认" / "x.jpg"
    assert holding.exists() and holding.read_bytes() == b"dup", d
    moved = client._tmp / "工作文件" / "刘音" / "会议" / "y.jpg"
    assert moved.exists() and moved.read_bytes() == b"other", d
    assert not (fam_dir / "x.jpg").exists() and not (fam_dir / "y.jpg").exists()
    print("folder approve dup goes holding: PASS")


def test_folder_approve_no_category_status_only(client):
    mac = client._tmp / "下载" / "Wangzhen-Mac"
    src = _make_file(mac / "项目开发" / "z.png", b"z")
    _add_review(client, "low_conf", {"src": src, "category": "99-待确认/04-低置信",
                                     "confidence": 0.4, "reason": "no-text:待确认",
                                     "evidence": "", "suggested_dst": "", "tags": []})
    r = client.post("/api/review/folders/approve",
                    json={"folder": "mac-backup:项目开发"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 1 and d["results"][0]["moved"] is False, d
    assert (mac / "项目开发" / "z.png").exists()
    print("folder approve no category status-only: PASS")


def test_folder_approve_unknown_folder_empty(client):
    r = client.post("/api/review/folders/approve",
                    json={"folder": "mac-backup:不存在的文件夹",
                          "category": "工作"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 0 and d["results"] == [], d
    print("folder approve unknown folder empty: PASS")


def test_folder_approve_invalid_category_400(client):
    r = client.post("/api/review/folders/approve",
                    json={"folder": "mac-backup:项目开发", "category": "不存在"})
    assert r.status_code == 400, r.text
    print("folder approve invalid category 400: PASS")


def test_review_page_has_folder_cards(client):
    from api.deps import mark_setup_done  # noqa: E402 - 懒加载，见文件头说明
    mark_setup_done()
    r = client.get("/review")
    assert r.status_code == 200, r.text
    assert "folder-card" in r.text and "批准整包" in r.text, r.text[:500]
    assert "/api/review/folders" in r.text
    print("review page has folder cards: PASS")
