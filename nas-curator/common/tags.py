#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""common.tags —— 标签生成：分类 → 业务类型、实体 → 机构、日期 → 年份、
媒体分类 → 场景。只用标准库；日期抽取用轻量正则（不依赖 curator_core，
避免循环导入；curator_core 传入已抽到的实体/日期时优先用传入值）。

标签种类：entity / year / biztype / person / scene / custom。
返回的每条标签为 (name, kind, confidence, source)，source 恒为 'auto'。
"""
from __future__ import annotations

import datetime
import os
import re
import struct

# v3.1.1 文件名业务类型优先词表（与 curator_core.best_category 共用同一份语义）。
BUSINESS_TYPES = [
    "合同", "协议", "授权书", "申报表", "发票", "简历", "周报", "纪要",
    "招标", "中标", "花名册", "营业执照", "报价", "清单", "明细",
    "验收标准", "许可证", "批文", "通知", "函",
]

_TAG_KINDS = ("entity", "year", "biztype", "person", "scene", "custom")

_DATE_RE = re.compile(r"(20\d{2})[-/年]?(?:\d{1,2}[-/月]?\d{1,2})?")


def match_business_types(name):
    """文件名命中哪些业务类型词（按词长降序去重）。"""
    hits = [w for w in BUSINESS_TYPES if w in name]
    hits.sort(key=len, reverse=True)
    return hits


def _mp4_creation_ym(path):
    """mp4/mov mvhd creation_time（自 1904-01-01 的秒数，UTC）-> (year, month)。
    moov 可能在文件头或尾，各扫 1MB；找不到或无效返回 None。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    chunks = []
    try:
        with open(path, "rb") as f:
            chunks.append(f.read(1024 * 1024))
            if size > 1024 * 1024:
                f.seek(max(0, size - 1024 * 1024))
                chunks.append(f.read())
    except OSError:
        return None
    for buf in chunks:
        idx = buf.find(b"mvhd")
        while idx != -1:
            try:
                ver = buf[idx + 4]
                if ver == 1:
                    ts = struct.unpack(">Q", buf[idx + 8:idx + 16])[0]
                else:
                    ts = struct.unpack(">I", buf[idx + 8:idx + 12])[0]
                base = datetime.datetime(1904, 1, 1, tzinfo=datetime.timezone.utc)
                dt = base + datetime.timedelta(seconds=ts)
                if 2000 <= dt.year <= 2030:
                    return (dt.year, dt.month)
            except Exception:
                pass
            idx = buf.find(b"mvhd", idx + 4)
    return None


def exif_datetime(path):
    """读拍摄日期 (year, month)：照片读 EXIF DateTimeOriginal（无则 DateTime）；
    视频读 mp4/mov mvhd creation_time。都读不到返回 None。
    PIL 为懒加载（容器已装，pillow_heif 负责 HEIC）；任何失败返回 None，不抛错。
    年份限定 2000–2030、月份 1–12，超出视为无效（相机时钟错）。"""
    if not path or not isinstance(path, str):
        return None
    ext = os.path.splitext(path)[1].lower()
    _PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp",
                   ".tif", ".tiff"}
    _VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".3gp"}
    if ext in _PHOTO_EXTS:
        try:
            import pillow_heif  # noqa: F401 - 注册 HEIC opener
        except Exception:
            pass
        try:
            from PIL import Image
            with Image.open(path) as im:
                exif = im._getexif() or {}
                raw = exif.get(36867) or exif.get(306)  # DateTimeOriginal / DateTime
                if raw:
                    parts = str(raw).replace("-", ":").split(":")
                    if len(parts) >= 2:
                        y, m = int(parts[0]), int(parts[1])
                        if 2000 <= y <= 2030 and 1 <= m <= 12:
                            return (y, m)
        except Exception:
            return None
        return None
    if ext in _VIDEO_EXTS:
        try:
            return _mp4_creation_ym(path)
        except Exception:
            return None
    return None


def extract_year(name="", text="", mtime=None):
    """年份：文件名/正文里的 20xx 年优先；都没有时用 mtime 的年份；都没有返回 None。"""
    for s in (name or "", text or ""):
        m = _DATE_RE.search(s)
        if m:
            return m.group(1)
    if mtime:
        import datetime
        try:
            return str(datetime.datetime.fromtimestamp(mtime).year)
        except (OSError, OverflowError, ValueError):
            return None
    return None


def gen_document_tags(category="", entities=(), date_str=None,
                      name="", text="", mtime=None):
    """文档自动标签：业务类型 + 机构 + 年份。"""
    tags = []
    for w in match_business_types(name or ""):
        tags.append((w, "biztype", 0.9, "auto"))
    for ent in entities or ():
        if ent:
            tags.append((str(ent), "entity", 0.8, "auto"))
    year = date_str or extract_year(name, text, mtime)
    if year:
        tags.append((str(year), "year", 0.9, "auto"))
    return _dedupe(tags)


def gen_media_tags(category="", kind="照片"):
    """媒体自动标签：分类即场景标签；kind 补一个媒介类型标签。"""
    tags = []
    if category:
        leaf = category.replace("\\", "/").rstrip("/").split("/")[-1]
        if leaf and leaf not in ("照片", "视频", "待确认", "模糊"):
            tags.append((leaf, "scene", 0.85, "auto"))
    if kind in ("照片", "视频"):
        tags.append((kind, "scene", 0.7, "auto"))
    return _dedupe(tags)


def _dedupe(tags):
    seen, out = set(), []
    for name, kind, conf, source in tags:
        key = (name, kind)
        if key in seen:
            continue
        seen.add(key)
        out.append((name, kind, conf, source))
    return out


def ensure_tag(conn, name, kind=None, color=None):
    from . import db as _db
    return _db.ensure_tag(conn, name, kind, color)


def attach_tags(conn, file_id, tags):
    """tags: list of (name, kind, confidence, source)。写 file_tags。"""
    from . import db as _db
    for name, kind, conf, source in tags:
        tid = _db.ensure_tag(conn, name, kind)
        conn.execute(
            """INSERT OR REPLACE INTO file_tags (file_id, tag_id, source, confidence)
               VALUES (?, ?, ?, ?)""",
            (file_id, tid, source, conf))
    conn.commit()
