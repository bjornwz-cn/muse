#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""common.hashing —— 文件指纹：SHA256 精确去重 + 感知哈希（dHash）近重复。

dHash 实现与 media-library-curator 的 curate.py 一致（灰度 → 9x8 → 相邻比较 → 64 位）。
只依赖标准库 + Pillow（PIL）。不引 imagehash 等新依赖。
"""
from __future__ import annotations

import hashlib
import os

_CHUNK = 1 << 20


def sha256_file(path, chunk=_CHUNK):
    """流式 SHA256，返回 hex；文件不可读时返回 None。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
    except OSError:
        return None
    return h.hexdigest()


def dhash_int(pil_image):
    """64 位 dHash：灰度 → 9x8 → 横向相邻像素比较。输入为 PIL Image。"""
    im = pil_image.convert("L").resize((9, 8))
    px = list(im.getdata())
    bits = 0
    for y in range(8):
        row = px[y * 9:(y + 1) * 9]
        for x in range(8):
            bits = (bits << 1) | (1 if row[x] > row[x + 1] else 0)
    return bits


def dhash_file(path):
    """对图片文件算 dHash；打不开/非图片返回 None。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return dhash_int(im)
    except Exception:
        return None


def hamming(a, b):
    """两个整数哈希的汉明距离。"""
    return bin(int(a) ^ int(b)).count("1")


def fingerprint(path):
    """(sha256, size, mtime)；文件不存在返回 (None, None, None)。"""
    try:
        st = os.stat(path)
    except OSError:
        return None, None, None
    return sha256_file(path), st.st_size, st.st_mtime
