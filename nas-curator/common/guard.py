#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""common.guard —— 排除规则硬执行（2026-11-19 王震两次纠正后口径）。

排除的只有：下载/Wangzhen-Mac/muse/（muse 工作文件夹）、下载/AI模型/
（模型文件）、下载/压缩包/（日后单独处理），另加与本次纠正无关的既有项
_统一分析报告、ocr-service。
下载/Wangzhen-Mac/ 本身是输入源（笔记本备份，只上传，NAS 侧整理安全），
绝不能排除；下载/压缩包/ 日后单独处理，P1 不作为输入源、不扫描，
但未进 guard 排除列表（见 config 注释与 P1 报告）。

规则来源：config/libraries.json 的 excluded 列表；文件缺失时用内置硬编码兜底
（双重保险：即使配置文件被改坏，muse/ 与 AI模型/ 也绝不会被整理）。
CLI 的每个入口（inventory / plan / apply / learn / analyze / inbox / process）
都必须先调 assert_allowed(root)，plan/apply 还要对每条 src 调 is_excluded 过滤。
"""
from __future__ import annotations

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(_HERE)
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "libraries.json")

# 硬编码兜底（与 libraries.json 保持一致；配置文件只是可读说明，真正执行靠这份）。
HARDCODED_EXCLUDED = (
    ("/vol1/1000/下载/Wangzhen-Mac/muse", "muse 工作文件夹，避免正在进行的工作被移走"),
    ("/vol1/1000/下载/AI模型", "模型文件，不动"),
    ("/vol1/1000/下载/压缩包", "压缩包日后单独处理"),
    ("/vol1/1000/_统一分析报告", "报告区"),
    ("/vol1/1000/ocr-service", "服务目录"),
)


def _norm(p):
    return os.path.abspath(os.path.expanduser(p))


def load_excluded():
    """合并配置文件 + 硬编码兜底，去重后返回 [(path, reason)]。"""
    out, seen = [], set()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        for e in cfg.get("excluded", []):
            p = _norm(e["path"])
            if p not in seen:
                seen.add(p)
                out.append((p, e.get("reason", "")))
    except (OSError, ValueError):
        pass
    for p, reason in HARDCODED_EXCLUDED:
        p = _norm(p)
        if p not in seen:
            seen.add(p)
            out.append((p, reason))
    return out


def is_excluded(path):
    """path 是否在任一排除前缀之下（等于前缀本身也算）。返回 (True, reason) 或 (False, '')。"""
    p = _norm(path)
    for prefix, reason in load_excluded():
        if p == prefix or p.startswith(prefix + os.sep):
            return True, reason
    return False, ""


def assert_allowed(path, what="目录"):
    """被排除则抛 ValueError（CLI 捕获后拒绝执行并打印原因）。"""
    bad, reason = is_excluded(path)
    if bad:
        raise ValueError(f"拒绝执行：{what} {path} 在排除列表内（{reason}）。"
                         "请把要归档的文件先移入统一收件箱 /vol1/1000/收件箱。")
    return True
