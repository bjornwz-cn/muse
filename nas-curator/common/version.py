#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""common.version —— 应用版本号（0.1.9）：VERSION 文件读写与语义比较。

VERSION 文件（项目根目录，JSON）：{"version": "0.1.9", "date": "2026-11-23",
"notes": "..."}。构建时 COPY 进镜像 /app/VERSION，运行时 read_local_version()
即当前版本；升级检查拿它与 GitHub 仓 nas-curator/VERSION 的最新版比较。
比较规则：按数字段比较（0.1.10 > 0.1.9），允许 "v" 前缀与 "-beta" 这类后缀
（后缀只影响相等判定中的预发布排序：带后缀 < 同数字正式版）。
"""
from __future__ import annotations

import json
import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_FILE = os.path.join(PROJECT_ROOT, "VERSION")

_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+){0,3})(?:-([0-9A-Za-z.-]+))?$")


def parse_version(s) -> tuple:
    """解析版本号 → (数字段 tuple, 后缀或 None)。非法时抛 ValueError。"""
    m = _VERSION_RE.match(str(s or "").strip())
    if not m:
        raise ValueError(f"非法版本号：{s!r}")
    nums = tuple(int(x) for x in m.group(1).split("."))
    return nums, m.group(2)


def compare_versions(a, b) -> int:
    """比较版本号：a < b 返回 -1，相等 0，a > b 返回 1。

    数字段按位比较，短的补 0（如 0.1 == 0.1.0）；数字相等时带后缀的
    （如 0.1.9-beta）小于不带后缀的正式版。
    """
    na, sa = parse_version(a)
    nb, sb = parse_version(b)
    n = max(len(na), len(nb))
    pa = na + (0,) * (n - len(na))
    pb = nb + (0,) * (n - len(nb))
    if pa != pb:
        return -1 if pa < pb else 1
    if sa == sb:
        return 0
    if sa is None:  # a 正式版，b 预发布 → a 更新
        return 1
    if sb is None:
        return -1
    return -1 if sa < sb else 1


def parse_version_payload(data) -> dict:
    """校验 VERSION 文件内容（dict），返回归一化后的 {version, date, notes}。

    version 必填且须合法；date/notes 可缺（缺省为空串）。非法抛 ValueError。
    """
    if not isinstance(data, dict):
        raise ValueError("VERSION 文件内容须为 JSON 对象")
    version = str(data.get("version") or "").strip()
    if not version:
        raise ValueError("VERSION 文件缺少 version 字段")
    parse_version(version)  # 校验格式
    return {
        "version": version,
        "date": str(data.get("date") or "").strip(),
        "notes": str(data.get("notes") or "").strip(),
    }


def read_local_version(version_file: str = VERSION_FILE) -> dict:
    """读本地 VERSION 文件；文件缺失/损坏时返回 0.0.0-dev（不抛错，保证接口不阻塞）。"""
    try:
        with open(version_file, encoding="utf-8") as f:
            return parse_version_payload(json.load(f))
    except (OSError, ValueError):
        return {"version": "0.0.0-dev", "date": "", "notes": ""}
