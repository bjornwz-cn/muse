#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0.1.5：图片扩展名配置（common.image_ext）。

缺省为两处旧硬编码列表的并集（16 个）；校验归一化（小写、补点、去重）；
配置写 libraries.json 后按 mtime 重读；非法配置回缺省不抛错。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from common import image_ext as IE  # noqa: E402


@pytest.fixture()
def cfgdir(tmp_path, monkeypatch):
    d = tmp_path / "config"
    d.mkdir()
    monkeypatch.setenv("CURATOR_CONFIG_DIR", str(d))
    IE.clear_cache()
    yield d
    IE.clear_cache()


def _write_cfg(d, image_extensions):
    cfg = {"libraries": [], "model_endpoints": {}}
    if image_extensions is not None:
        cfg["image_extensions"] = image_extensions
    (d / "libraries.json").write_text(json.dumps(cfg, ensure_ascii=False),
                                       encoding="utf-8")


def test_default_is_union_of_both_old_lists(cfgdir):
    # 无配置文件：缺省 16 个 = 文档引擎 10 个 + 媒体引擎多出的 6 个
    exts = IE.get_image_exts()
    assert exts == IE.DEFAULT_IMAGE_EXTS
    assert len(exts) == 16
    for e in (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp",
              ".gif", ".heic", ".heif", ".jfif", ".nef", ".cr2", ".arw",
              ".dng", ".livp"):
        assert e in exts


def test_validate_normalizes_and_dedupes():
    assert IE.validate_image_exts([".JPG", "png", " .jpeg ", ".jpg"]) == [
        ".jpg", ".png", ".jpeg"]
    assert IE.validate_image_exts([".avif"]) == [".avif"]


def test_validate_rejects():
    for bad in ([], ["."], [".jpg!"], [".a" * 11], [123], "not-a-list",
                [".jpg"] * 51):
        with pytest.raises(ValueError):
            IE.validate_image_exts(bad)


def test_load_from_libraries_json(cfgdir):
    _write_cfg(cfgdir, [".jpg", ".png", ".avif"])
    assert IE.get_image_exts() == frozenset({".jpg", ".png", ".avif"})


def test_reload_on_mtime_change(cfgdir):
    _write_cfg(cfgdir, [".jpg"])
    assert IE.get_image_exts() == frozenset({".jpg"})
    import time
    time.sleep(0.01)
    _write_cfg(cfgdir, [".png", ".webp"])
    assert IE.get_image_exts() == frozenset({".png", ".webp"})


def test_invalid_config_falls_back_to_default(cfgdir):
    _write_cfg(cfgdir, [".jpg!"])
    assert IE.get_image_exts() == IE.DEFAULT_IMAGE_EXTS
    (cfgdir / "libraries.json").write_text("{broken", encoding="utf-8")
    IE.clear_cache()
    assert IE.get_image_exts() == IE.DEFAULT_IMAGE_EXTS


def test_textkit_honors_config(tmp_path, cfgdir, monkeypatch):
    """文档引擎抽取：.gif 移出配置后按 skip 走；新加 .avif 按图片走 OCR 分支。"""
    from curator_core import textkit as TK
    _write_cfg(cfgdir, [".jpg", ".png", ".avif"])  # 无 .gif
    IE.clear_cache()
    # extract_text 先开文件再判扩展名，须用真实文件
    for name in ("x.gif", "x.avif", "x.jpg"):
        (tmp_path / name).write_bytes(b"\x89PNG\r\n\x1a\n")
    t, src = TK.extract_text(str(tmp_path / "x.gif"), ".gif", allow_ocr=False)
    assert (t, src) == ("", "skip")
    t, src = TK.extract_text(str(tmp_path / "x.avif"), ".avif", allow_ocr=False)
    assert (t, src) == ("", "image")
    t, src = TK.extract_text(str(tmp_path / "x.jpg"), ".jpg", allow_ocr=False)
    assert (t, src) == ("", "image")


def test_media_is_media_honors_config(cfgdir):
    """媒体引擎判定：配置里的扩展名才认作照片。"""
    from media_core import curate
    _write_cfg(cfgdir, [".jpg", ".png", ".avif"])
    IE.clear_cache()
    assert curate.is_media("x.avif") == "照片"
    assert curate.is_media("x.gif") is None
    assert curate.is_media("x.mp4") == "视频"
    assert ".avif" in curate.media_exts()
    assert ".gif" not in curate.media_exts()
