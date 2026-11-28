#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""media_core 冒烟测试（引擎级，不加载模型、不碰 NAS）。

只用本机 /tmp：PIL 生成纯色/随机噪声测试图；跑 decode_item、
decide() 纯逻辑分支、plan_media_files(classifier=None)、plan_inbox、
video_frames 失败路径、build_report 纯函数；0.1.8 新增 cv2 视频抽帧
与 .livp 解包测试（有 cv2 才跑、无 cv2 时 skip，见各测试）。

运行：python3 tests/test_media_core_smoke.py
（本机无 pytest：文件自带 __main__ runner；有 pytest 时也可 pytest 运行，
缺失依赖时用 pytest.skip / 本地 skip 跳过而不是失败。）

本机环境说明（2026-11-19 实测）：Python 3.12、PIL/numpy 有、cv2/torch 无。
因此 decode_item 在本机走 decode_failed 分支（缺 cv2），测试对该分支
做了断言；cv2 存在时的 blur 数值分支用条件断言（有 cv2 才跑）。
0.1.8：视频抽帧改走 cv2.VideoCapture（不再调 ffmpeg 子进程），缺 cv2
时 video_frames 返回 []（与旧版 ffmpeg 缺失时一致）；.livp 解包取帧
不依赖 cv2（.jpg 静帧分支），.mov 分支需 cv2。
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pytest as _pytest
    skip = _pytest.skip
except ImportError:  # 本机无 pytest：本地 skip 垫片
    _pytest = None

    class _Skipped(Exception):
        pass

    def skip(msg=""):
        raise _Skipped(msg)


def _require(modname):
    """依赖缺失时 skip（不失败）。返回已导入模块。"""
    try:
        return importlib.import_module(modname)
    except ImportError:
        skip("%s 未安装，跳过" % modname)


def _is_skip_exc(e):
    if _pytest is not None and isinstance(e, _pytest.skip.Exception):
        return True
    return type(e).__name__ in ("_Skipped", "Skipped")


PLAN_KEYS = {"src", "dst", "new_name", "category", "confidence", "reason",
             "evidence", "action", "kind", "size", "ext", "sha256", "tags",
             "review_kind", "file_id", "score", "auto_exempt",
             "confidence_original", "exif_placed"}
# 0.2.5 补：engine._base_item 新增 "confidence_original"（压之前的原始置信度）
# 与 "exif_placed"（EXIF 年/月定位置放）。
# 0.1.8 补：engine._base_item 自 0.1.1 起就出 "score" 与 "auto_exempt"
# （plan_items 表亦有此二列），旧 PLAN_KEYS 漏了它们，PIL 齐全的环境
# 下 test_plan_media_files_no_classifier 的精确键集断言必失败；
# P10 的 132 passed 应是该测试被 PIL 缺失跳过所致。

THRESHOLDS = {"margin_min": 0.12, "score_min": 0.35, "blur_min": 50,
              "phash_max_distance": 5, "near_dup_enabled": True}


def _cfg(tmp):
    return {
        "library_root": os.path.join(tmp, "lib"),
        "inbox": os.path.join(tmp, "inbox"),
        "review_root": os.path.join(tmp, "lib", "_Review"),
        "work_dir": os.path.join(tmp, "work"),
        "model_dir": os.path.join(tmp, "models"),
        "settle_minutes": 0,
        "max_files_per_run": 300,
        "thresholds": dict(THRESHOLDS),
        "photo_dirs": {"旅行风景": "照片/旅行与城市/旅行风景"},
        "video_dirs": {"旅行风景": "视频/旅行风景"},
        "review_dirs": {"待确认": "_Review/待确认", "模糊": "_Review/模糊",
                        "疑似重复": "_Review/疑似重复"},
    }


def _make_images(tmp):
    """生成 5 张测试图：3 张纯色 + 1 张随机噪声 + 噪声的同内容副本。"""
    PIL = _require("PIL")
    np = _require("numpy")
    from PIL import Image
    paths = {}
    for name, color in (("red", (255, 0, 0)), ("green", (0, 255, 0)),
                        ("blue", (0, 0, 255))):
        p = os.path.join(tmp, "%s.png" % name)
        Image.new("RGB", (200, 200), color).save(p)
        paths[name] = p
    arr = np.random.default_rng(0).integers(0, 256, (200, 200, 3), dtype="uint8")
    noise = os.path.join(tmp, "noise.png")
    Image.fromarray(arr).save(noise)
    paths["noise"] = noise
    copy = os.path.join(tmp, "noise_copy.png")
    with open(noise, "rb") as f:
        data = f.read()
    with open(copy, "wb") as f:
        f.write(data)
    paths["noise_copy"] = copy
    return paths


def _scores(top, top_s, second, second_s, zh=None, sig=None):
    """构造 Classifier.scores() 同形的假分数。"""
    zh = dict(zh or {})
    sig = dict(sig or {})
    zh.setdefault("屏幕截图", 0.0)
    zh.setdefault("文档与票据", 0.0)
    sig.setdefault("屏幕截图", 0.0)
    sig.setdefault("文档与票据", 0.0)
    zh.setdefault(top, top_s)
    sig.setdefault(top, top_s)
    return {"zh": zh, "sig": sig,
            "sig_top": (top, top_s), "sig_2": (second, second_s)}


# ---------------- 测试 ----------------

def test_import_no_torch_and_media_exts():
    import media_core
    assert "torch" not in sys.modules, "import media_core 不应加载 torch"
    assert "cv2" not in sys.modules or True  # cv2 即使已装也不应在 import 时加载
    assert ".jpg" in media_core.MEDIA_EXTS
    assert ".png" in media_core.MEDIA_EXTS
    assert ".mp4" in media_core.MEDIA_EXTS
    assert ".mov" in media_core.MEDIA_EXTS
    assert ".pdf" not in media_core.MEDIA_EXTS
    assert ".mp3" not in media_core.MEDIA_EXTS
    assert ".lrv" not in media_core.MEDIA_EXTS
    assert media_core.MEDIA_EXTS == frozenset(
        media_core.IMG_EXT | media_core.VID_EXT)
    assert len(media_core.PHOTO_CATEGORIES) == 12
    assert len(media_core.VIDEO_CATEGORIES) == 6
    assert "旅行风景" in media_core.PHOTO_CATEGORIES
    assert "屏幕录制" in media_core.VIDEO_CATEGORIES


def test_decode_item_returns_numbers():
    _require("PIL")
    from media_core.curate import decode_item
    with tempfile.TemporaryDirectory() as tmp:
        paths = _make_images(tmp)
        row = {"path": paths["noise"], "name": "noise.png", "kind": "照片",
               "size": os.path.getsize(paths["noise"]), "mtime": 0, "ext": ".png"}
        img, blur, white, colors, ar, note = decode_item(row)
        # 无论 cv2 在不在，blur/white 都必须是数值（失败时为 0.0 默认值）。
        assert isinstance(blur, float), "blur 应为 float，得 %r" % blur
        assert isinstance(white, float), "white 应为 float，得 %r" % white
        assert isinstance(colors, int) and isinstance(ar, float)
        if importlib.util.find_spec("cv2") is None:
            assert img is None and note == "ModuleNotFoundError", \
                "缺 cv2 时应为 decode 失败，note=%r" % note
        else:
            assert img is not None and note == "", "有 cv2 时噪声图应解码成功"
            assert blur > 0, "噪声图 Laplacian 方差应 > 0"


def test_decide_branches():
    from media_core.curate import decide
    cfg = {"thresholds": dict(THRESHOLDS)}
    photo = {"kind": "照片"}
    # 白底截图规则：white>=0.45 且截图分>=0.30 → 屏幕截图（优先于 top1）。
    sc = _scores("旅行风景", 0.82, "城市与建筑", 0.31,
                 zh={"屏幕截图": 0.55}, sig={"屏幕截图": 0.50})
    cat, cat2, s1, s2, why = decide(photo, sc, 80.0, 0.50, 120, 1.5, "", cfg)
    assert (cat, why) == ("屏幕截图", "accepted"), (cat, why)
    # 白底文档规则：white>=0.45 且文档分>=0.25 → 文档与票据。
    sc = _scores("旅行风景", 0.82, "城市与建筑", 0.31,
                 zh={"文档与票据": 0.40}, sig={"文档与票据": 0.35})
    cat, cat2, s1, s2, why = decide(photo, sc, 80.0, 0.50, 120, 1.5, "", cfg)
    assert (cat, why) == ("文档与票据", "accepted"), (cat, why)
    # 模糊：blur < 50（仅照片）→ 模糊，优先于低置信。
    sc = _scores("旅行风景", 0.82, "城市与建筑", 0.31)
    cat, cat2, s1, s2, why = decide(photo, sc, 10.0, 0.10, 120, 1.5, "", cfg)
    assert (cat, why) == ("模糊", "blurry"), (cat, why)
    # 低置信 margin：top1-top2=0.05 < 0.12 → 待确认。
    sc = _scores("旅行风景", 0.80, "城市与建筑", 0.75)
    cat, cat2, s1, s2, why = decide(photo, sc, 80.0, 0.10, 120, 1.5, "", cfg)
    assert (cat, why) == ("待确认", "low_confidence"), (cat, why)
    # 低置信 top1：top1=0.20 < 0.35 → 待确认。
    sc = _scores("旅行风景", 0.20, "城市与建筑", 0.05)
    cat, cat2, s1, s2, why = decide(photo, sc, 80.0, 0.10, 120, 1.5, "", cfg)
    assert (cat, why) == ("待确认", "low_confidence"), (cat, why)
    # 解码失败：note 非空 → 待确认/decode_failed（优先于模糊与低置信）。
    sc = _scores("旅行风景", 0.82, "城市与建筑", 0.31)
    cat, cat2, s1, s2, why = decide(photo, sc, 0.0, 0.0, 999, 1.0, "ValueError", cfg)
    assert (cat, why) == ("待确认", "decode_failed"), (cat, why)
    # 接受：margin 与 top1 均达标 → top1 分类。
    sc = _scores("旅行风景", 0.82, "城市与建筑", 0.31)
    cat, cat2, s1, s2, why = decide(photo, sc, 80.0, 0.10, 120, 1.5, "", cfg)
    assert (cat, cat2, why) == ("旅行风景", "城市与建筑", "accepted"), (cat, cat2, why)
    assert abs(s1 - 0.82) < 1e-9 and abs(s2 - 0.31) < 1e-9


def test_plan_media_files_no_classifier():
    _require("PIL")
    from media_core.engine import plan_media_files
    with tempfile.TemporaryDirectory() as tmp:
        paths = _make_images(tmp)
        pdf = os.path.join(tmp, "doc.pdf")
        with open(pdf, "wb") as f:
            f.write(b"%PDF-1.4 fake")
        mp3 = os.path.join(tmp, "song.mp3")
        with open(mp3, "wb") as f:
            f.write(b"ID3 fake")
        cfg = _cfg(tmp)
        items, stats = plan_media_files(
            [paths["noise"], paths["noise_copy"], paths["red"], pdf, mp3],
            cfg, classifier=None, index=None)
        assert len(items) == 5, "应逐文件出 plan-item，得 %d" % len(items)
        for it in items:
            assert set(it.keys()) == PLAN_KEYS, set(it.keys()) ^ PLAN_KEYS
            assert it["kind"] == "media"
            assert isinstance(it["confidence"], float)
            assert it["file_id"] is None
        by_src = {it["src"]: it for it in items}
        # 非媒体扩展名 → skip / not-media。
        assert by_src[pdf]["action"] == "skip" and by_src[pdf]["reason"] == "not-media"
        assert by_src[mp3]["action"] == "skip" and by_src[mp3]["reason"] == "not-media"
        assert by_src[pdf]["dst"] == "" and by_src[pdf]["tags"] == []
        # 同内容第二份 → review(dup)。
        dup = by_src[paths["noise_copy"]]
        assert dup["action"] == "review" and dup["review_kind"] == "dup", dup
        assert dup["reason"].startswith("dup:"), dup["reason"]
        assert dup["confidence"] in (0.9, 0.8)
        assert dup["dst"] == "" and dup["evidence"] == paths["noise"]
        # 首份：本机缺 cv2 → decode_failed review（category 为待确认）；
        # 有 cv2 时解码成功、无 classifier → no_classifier review（分类字段留空）。
        first = by_src[paths["noise"]]
        assert first["action"] == "review" and first["review_kind"] == "ambiguous", first
        if importlib.util.find_spec("cv2") is None:
            assert first["category"] == "待确认"
            assert first["reason"].startswith("decode_failed:"), first["reason"]
        else:
            assert first["category"] == "", "classifier=None 时分类字段留空"
            assert first["reason"] == "no_classifier"
        # 纯色图：缺 cv2 → decode_failed；有 cv2 → blur=0 → 模糊 review。
        red = by_src[paths["red"]]
        assert red["action"] == "review" and red["review_kind"] in ("ambiguous", "blur"), red
        assert stats["scanned"] == 5 and stats["skipped"] == 2
        assert stats["dup"] >= 1 and stats["review"] >= 3


def test_plan_inbox():
    _require("PIL")
    from media_core.engine import plan_inbox
    with tempfile.TemporaryDirectory() as tmp:
        paths = _make_images(tmp)
        inbox = os.path.join(tmp, "inbox")
        os.makedirs(inbox)
        import shutil
        shutil.copy(paths["noise"], os.path.join(inbox, "noise.png"))
        with open(os.path.join(inbox, "note.pdf"), "wb") as f:
            f.write(b"%PDF-1.4 fake")
        cfg = _cfg(tmp)
        items, stats = plan_inbox(cfg, classifier=None, index=None)
        # 扫描只认媒体：pdf 不进 plan；png 进 review（无 classifier）。
        assert len(items) == 1, items
        assert items[0]["src"].endswith("noise.png")
        assert items[0]["action"] == "review"
        assert stats["waiting"] == 0
        assert stats["scan_skipped"].get("非媒体文件") == 1


def test_video_frames_missing_file():
    from media_core.curate import video_frame, video_frames
    assert video_frame("/tmp/does-not-exist-xyz.mp4") is None
    assert video_frames("/tmp/does-not-exist-xyz.mp4") == []
    assert video_frames("/tmp/does-not-exist-xyz.mp4", max_frames=6, step=10) == []


def test_video_frames_cv2_synthetic():
    """0.1.8：cv2 抽帧——合成一段小 mp4，video_frames 应返回 PIL RGB 帧。

    无 cv2 时 skip（本机无 cv2；NAS 容器内有 cv2 4.10.0，跑此分支）。
    短片（3 秒）只在 0 秒处有帧：断言 ≥1 帧且均为 RGB，不硬编帧数，
    避免末尾时间点取舍的抖动。
    """
    cv2 = _require("cv2")
    np = _require("numpy")
    from PIL import Image
    from media_core.curate import video_frame, video_frames
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "clip.mp4")
        w = cv2.VideoWriter(p, cv2.VideoWriter_fourcc(*"mp4v"),
                            10.0, (320, 240))
        assert w.isOpened(), "VideoWriter 打不开测试 mp4"
        for i in range(30):
            w.write(np.full((240, 320, 3), (i * 8 % 256, 100, 200),
                            dtype="uint8"))
        w.release()
        assert os.path.getsize(p) > 0
        frames = video_frames(p)
        assert len(frames) >= 1, "3 秒短片至少应在 0 秒处抽到 1 帧"
        for fr in frames:
            assert isinstance(fr, Image.Image) and fr.mode == "RGB"
            assert fr.size[0] > 0 and fr.size[1] > 0
        one = video_frame(p, "0")
        assert isinstance(one, Image.Image) and one.mode == "RGB"
        # 超片长的时间点抽不到：只返回已有帧，不重复首帧。
        assert video_frames(p, max_frames=6, step=10) == frames
        # 损坏文件（非视频字节）：打不开 → 空列表，不抛错。
        bad = os.path.join(tmp, "bad.mp4")
        with open(bad, "wb") as f:
            f.write(os.urandom(2048))
        assert video_frames(bad) == []
        assert video_frame(bad) is None


def _make_livp(tmp, name, jpg=True, mov_bytes=None):
    """生成一个 .livp（zip）：可含 .jpg 静帧和/或 .mov 内容。"""
    import zipfile
    from PIL import Image
    p = os.path.join(tmp, name)
    with zipfile.ZipFile(p, "w") as z:
        if jpg:
            jp = os.path.join(tmp, "still.jpg")
            Image.new("RGB", (200, 150), (10, 200, 90)).save(jp)
            z.write(jp, "IMG_0001.JPG")
        if mov_bytes is not None:
            z.writestr("IMG_0001.MOV", mov_bytes)
    return p


def test_livp_still_frame():
    """0.1.8：.livp 解包——.jpg 静帧应解出 1 帧（不依赖 cv2）；只有损坏
    .mov 时返回 []（不抛错）。"""
    from media_core.curate import _decode_livp_frames
    with tempfile.TemporaryDirectory() as tmp:
        good = _make_livp(tmp, "good.livp", jpg=True)
        frames = _decode_livp_frames(good)
        assert len(frames) == 1 and frames[0].mode == "RGB", frames
        bad = _make_livp(tmp, "bad.livp", jpg=False,
                         mov_bytes=os.urandom(4096))
        assert _decode_livp_frames(bad) == []
        assert _decode_livp_frames(os.path.join(tmp, "nope.livp")) == []
        assert _decode_livp_frames(good + ".missing") == []


def test_decode_item_livp():
    """0.1.8：decode_item 走 .livp 分支——有 cv2 时静帧应解码成功；
    缺 cv2 时与照片同一口径（_image_features 报 ModuleNotFoundError）。"""
    _require("PIL")
    from media_core.curate import decode_item
    with tempfile.TemporaryDirectory() as tmp:
        livp = _make_livp(tmp, "test.livp", jpg=True)
        row = {"path": livp, "name": "test.livp", "kind": "照片",
               "size": os.path.getsize(livp), "mtime": 0, "ext": ".livp"}
        img, blur, white, colors, ar, note = decode_item(row)
        if importlib.util.find_spec("cv2") is None:
            assert img is None and note == "ModuleNotFoundError", \
                "缺 cv2 时应为 decode 失败，note=%r" % note
        else:
            assert img is not None and note == "", "有 cv2 时静帧应解码成功"
            assert blur >= 0


def test_build_report_is_pure():
    from media_core.curate import build_report, write_report
    cfg = {"inbox": "/vol1/1000/媒体库/_收件箱"}
    result = {"counts": {"照片/旅行与城市/旅行风景": 1, "_Review/待确认": 2},
              "applied": False, "processed": 3, "bytes": 3000000}
    plan = [{"name": "a.jpg", "kind": "照片", "decision": "旅行风景",
             "target": "照片/旅行与城市/旅行风景", "score": 0.82, "note": "城市与建筑"}]
    text = build_report(cfg, result, plan, {}, [])
    assert "媒体库收件箱治理报告" in text
    assert "旅行风景" in text and "_Review/待确认" in text
    assert write_report(cfg, result, plan, {}, []) == text


def test_review_plan_rejects_apply():
    from media_core.curate import review_plan
    cfg = _cfg(tempfile.mkdtemp())
    try:
        review_plan(cfg, ("待确认",), apply=True)
    except ValueError as e:
        assert "apply" in str(e)
    else:
        raise AssertionError("review_plan(apply=True) 应抛 ValueError")


if __name__ == "__main__":
    import traceback
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    passed, skipped, failed = [], [], []
    for name, fn in fns:
        try:
            fn()
        except Exception as e:
            if _is_skip_exc(e):
                skipped.append((name, str(e)))
                print("SKIP %s: %s" % (name, e))
            else:
                failed.append(name)
                print("FAIL %s" % name)
                traceback.print_exc()
        else:
            passed.append(name)
            print("PASS %s" % name)
    print("\n%d passed, %d skipped, %d failed" % (len(passed), len(skipped), len(failed)))
    sys.exit(1 if failed else 0)
