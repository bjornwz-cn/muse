#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0.1.5：文件预览 / 下载端点（api.files）。

只允许 CURATOR_FILE_ROOT（缺省 /vol1/1000）下的文件；
realpath 校验防路径穿越（符号链接也算）；不存在/目录/越界一律 4xx；
图片与 PDF 内联显示，其余下载。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

# 注意：api.main 不在模块顶层 import——api/deps.py 在 import 时就把
# CURATOR_CONFIG_DIR 冻结为 CONFIG_DIR；test_wizard.py 依赖在模块 import 前
# 先设该环境变量。顶层 import api.main 会让 wizard 测试写错配置目录
# （P4 回归曾因此失败；0.1.5 曾因此重演）。与 test_api_smoke /
# test_web_flow / test_apply_gating 一致，api 模块只在函数内懒加载。

# 最小 PNG（1x1 透明）与纯文本
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0bIDATx\x9cc\x00\x01"
       b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def _client(tmp_path, monkeypatch):
    from api.main import create_app  # noqa: E402 - 懒加载，见文件头说明
    root = tmp_path / "vol1" / "1000"
    (root / "照片").mkdir(parents=True)
    (root / "照片" / "a.png").write_bytes(PNG)
    (root / "doc.txt").write_text("hello", encoding="utf-8")
    (root / "子目录").mkdir()
    monkeypatch.setenv("CURATOR_FILE_ROOT", str(root))
    monkeypatch.setenv("CURATOR_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("CURATOR_CONFIG_DIR", str(tmp_path / "config"))
    return TestClient(create_app()), root


def test_preview_image_inline(tmp_path, monkeypatch):
    c, root = _client(tmp_path, monkeypatch)
    r = c.get("/api/file/preview", params={"path": str(root / "照片" / "a.png")})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert "inline" in r.headers["content-disposition"]
    assert r.content == PNG


def test_preview_text_downloads(tmp_path, monkeypatch):
    c, root = _client(tmp_path, monkeypatch)
    r = c.get("/api/file/preview", params={"path": str(root / "doc.txt")})
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert r.content == b"hello"


def test_download_forces_attachment(tmp_path, monkeypatch):
    c, root = _client(tmp_path, monkeypatch)
    r = c.get("/api/file/download", params={"path": str(root / "照片" / "a.png")})
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]


def test_preview_forces_download_param(tmp_path, monkeypatch):
    c, root = _client(tmp_path, monkeypatch)
    r = c.get("/api/file/preview",
              params={"path": str(root / "照片" / "a.png"), "download": "true"})
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]


def test_traversal_blocked(tmp_path, monkeypatch):
    c, root = _client(tmp_path, monkeypatch)
    # 绝对路径越界
    r = c.get("/api/file/preview", params={"path": "/etc/passwd"})
    assert r.status_code == 403
    # .. 穿越
    r = c.get("/api/file/preview",
              params={"path": str(root / "照片" / ".." / ".." / "etc" / "passwd")})
    assert r.status_code == 403
    # 符号链接穿越
    link = root / "evil.png"
    link.symlink_to("/etc/passwd")
    r = c.get("/api/file/preview", params={"path": str(link)})
    assert r.status_code == 403


def test_missing_and_directory(tmp_path, monkeypatch):
    c, root = _client(tmp_path, monkeypatch)
    r = c.get("/api/file/preview", params={"path": str(root / "不存在.png")})
    assert r.status_code == 404
    r = c.get("/api/file/preview", params={"path": str(root / "子目录")})
    assert r.status_code == 404
