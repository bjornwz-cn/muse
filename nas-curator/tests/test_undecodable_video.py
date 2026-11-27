#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-11-27 提速改动 3 回归：解不开的视频（RealVideo .rm/.rmvb/.ram）跳过解码。

覆盖：video_frames / decode_item_frames 对黑名单扩展名不碰 cv2、直接返回
无法验证；plan_media_files 把 .rmvb 记为 decode_failed:unsupported_codec、
进复核（不烧 CPU、不刷 RealVideo 报错）；非黑名单视频仍走正常解码路径；
已进复核的 .rmvb 文件下轮靠复核行去重（改动 2）不再重试。
只用 tmp 目录与临时 DB，不碰 NAS。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import curator as CUR  # noqa: E402
from common import db as DB  # noqa: E402
from media_core.curate import (  # noqa: E402
    UNDECODABLE_VIDEO_EXTS,
    decode_item_frames,
    video_frames,
)
from media_core.engine import plan_media_files  # noqa: E402


def _cfg(tmp):
    return {
        "library_root": os.path.join(tmp, "lib"),
        "inbox": os.path.join(tmp, "inbox"),
        "review_root": os.path.join(tmp, "lib", "_Review"),
        "work_dir": os.path.join(tmp, "work"),
        "model_dir": os.path.join(tmp, "models"),
        "settle_minutes": 0,
        "thresholds": {"margin_min": 0.12, "score_min": 0.35, "blur_min": 50,
                       "phash_max_distance": 5, "near_dup_enabled": True},
        "photo_dirs": {}, "video_dirs": {},
        "review_dirs": {"待确认": "_Review/待确认", "模糊": "_Review/模糊",
                        "疑似重复": "_Review/疑似重复"},
    }


def _fake_video(path):
    with open(path, "wb") as f:
        f.write(os.urandom(256))
    return path


def test_blacklist_constant():
    assert UNDECODABLE_VIDEO_EXTS == {".rm", ".rmvb", ".ram"}


def test_video_frames_skips_realvideo(tmp_path):
    for ext in (".rm", ".rmvb", ".ram"):
        p = _fake_video(str(tmp_path / ("v" + ext)))
        assert video_frames(p) == []


def test_decode_item_frames_skips_realvideo(tmp_path):
    for ext in (".rm", ".rmvb", ".ram"):
        p = _fake_video(str(tmp_path / ("v" + ext)))
        frames, _b, _w, _c, _ar, note = decode_item_frames(
            {"path": p, "ext": ext, "kind": "视频"})
        assert frames == []
        assert note == "unsupported_codec:%s" % ext, note


def test_plan_media_files_rmvb_goes_to_review(tmp_path):
    p = _fake_video(str(tmp_path / "v.rmvb"))
    items, stats = plan_media_files([p], _cfg(str(tmp_path)), classifier=None)
    assert len(items) == 1
    it = items[0]
    assert it["action"] == "review"
    assert it["reason"] == "decode_failed:unsupported_codec:.rmvb", it
    assert it["review_kind"] == "ambiguous"
    assert stats["decode_failed"] == 1


def test_non_blacklist_video_still_attempts_decode(tmp_path):
    """非黑名单视频仍走正常解码路径（本机无 cv2 时报 noframe，不是 unsupported）。"""
    p = _fake_video(str(tmp_path / "v.mp4"))
    frames, _b, _w, _c, _ar, note = decode_item_frames(
        {"path": p, "ext": ".mp4", "kind": "视频"})
    assert frames == []
    assert not note.startswith("unsupported_codec"), note


def test_rmvb_not_retried_once_reviewed(tmp_path):
    """已进复核的 .rmvb 文件下轮靠复核行去重不再重试（改动 2 兜底）。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    p = _fake_video(str(inbox / "v.rmvb"))
    db = str(tmp_path / "t.db")
    conn = DB.connect(db)
    DB.init_db(db)
    DB.add_review(conn, plan_id=None, kind="ambiguous",
                  payload={"src": p, "size": os.path.getsize(p),
                           "mtime": os.path.getmtime(p),
                           "reason": "decode_failed:unsupported_codec:.rmvb"})
    cfg = _cfg(str(tmp_path))
    cfg_path = tmp_path / "media.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    args = argparse.Namespace(media_config=str(cfg_path))
    items, stats = CUR._dispatch_plan(str(inbox), "media", None, None, None,
                                      None, args, {}, learn_conn=conn)
    assert [i["src"] for i in items] == []
    assert stats["review_skipped"] == 1
    conn.close()
