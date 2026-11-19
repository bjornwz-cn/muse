#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 测试：排除断言（2026-11-19 王震两次纠正后口径）。

排除的只有：下载/Wangzhen-Mac/muse/（muse 工作文件夹）、下载/AI模型/（模型文件），
另加与纠正无关的既有项 _统一分析报告、ocr-service。
下载/Wangzhen-Mac/ 本身是输入源（笔记本备份，只上传），必须放行；
下载/压缩包/ 日后单独处理、P1 不作为输入源，但未进 guard 排除列表（见 config 注释）。
测四层：① guard.is_excluded 判定；② guard.assert_allowed；③ _resolve_library
对 Wangzhen-Mac/统一收件箱的 mixed 解析；④ 配置缺失时硬编码兜底。
只读测试，不动 NAS。
用法：python3 -m pytest tests/test_exclusion.py -v 或 python3 tests/test_exclusion.py
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
sys.path.insert(0, PROJECT_ROOT)

from common import guard  # noqa: E402

MUST_EXCLUDE = [
    "/vol1/1000/下载/Wangzhen-Mac/muse",
    "/vol1/1000/下载/Wangzhen-Mac/muse/",
    "/vol1/1000/下载/Wangzhen-Mac/muse/项目",
    "/vol1/1000/下载/AI模型",
    "/vol1/1000/下载/AI模型/qwen3",
    "/vol1/1000/_统一分析报告",
    "/vol1/1000/ocr-service",
]

MUST_ALLOW = [
    "/vol1/1000/工作文件",
    "/vol1/1000/工作文件/收件箱",
    "/vol1/1000/家庭文件",
    "/vol1/1000/家庭文件/收件箱",
    "/vol1/1000/媒体库",
    "/vol1/1000/媒体库/_收件箱",
    "/vol1/1000/媒体库/照片",
    "/vol1/1000/收件箱",
    # 笔记本备份输入源：整目录放行（只上传，NAS 侧整理安全）
    "/vol1/1000/下载/Wangzhen-Mac",
    "/vol1/1000/下载/Wangzhen-Mac/",
    "/vol1/1000/下载/Wangzhen-Mac/微信",
    "/vol1/1000/下载/Wangzhen-Mac/文稿",
    "/vol1/1000/下载/Wangzhen-Mac/润工作",
    "/vol1/1000/下载/Wangzhen-Mac/项目开发",
    "/vol1/1000/下载/Wangzhen-Mac/下载",
    "/vol1/1000/下载/Wangzhen-Mac/3D打印",
    "/vol1/1000/下载",
]

# 未进 guard 排除列表（日后单独处理，P1 不作为输入源、不扫描；显式手动 plan 仍可扫到）
NOT_GUARD_EXCLUDED = [
    "/vol1/1000/下载/压缩包",
]


def run():
    for p in MUST_EXCLUDE:
        bad, reason = guard.is_excluded(p)
        assert bad, f"应排除却放行：{p}"
        print(f"  排除 ✓ {p}（{reason}）")
        try:
            guard.assert_allowed(p)
        except ValueError:
            pass
        else:
            raise AssertionError(f"assert_allowed 应拒绝：{p}")
    for p in MUST_ALLOW:
        bad, _ = guard.is_excluded(p)
        assert not bad, f"应放行却排除：{p}"
        print(f"  放行 ✓ {p}")
        assert guard.assert_allowed(p) is True
    for p in NOT_GUARD_EXCLUDED:
        bad, _ = guard.is_excluded(p)
        assert not bad, f"压缩包/ 按定案不进 guard 排除列表（仅不作为输入源）：{p}"
        print(f"  不拦 ✓ {p}（未进 guard 排除列表，日后单独处理）")
    # 配置缺失时硬编码兜底仍生效：muse/ 与 AI模型/ 仍排除，Wangzhen-Mac/ 仍放行
    import common.guard as g
    real = g.CONFIG_PATH
    g.CONFIG_PATH = "/nonexistent/libraries.json"
    try:
        assert g.is_excluded("/vol1/1000/下载/Wangzhen-Mac/muse")[0]
        assert g.is_excluded("/vol1/1000/下载/AI模型")[0]
        assert not g.is_excluded("/vol1/1000/下载/Wangzhen-Mac")[0]
        print("  兜底 ✓ 配置缺失时硬编码仍排除 muse/ 与 AI模型/，放行 Wangzhen-Mac/")
    finally:
        g.CONFIG_PATH = real
    # _resolve_library：输入源与统一收件箱解析为 mixed
    import curator as CUR
    for root, want_id, want_kind in [
            ("/vol1/1000/下载/Wangzhen-Mac", "mac-backup", "mixed"),
            ("/vol1/1000/收件箱", "inbox", "mixed"),
            ("/vol1/1000/工作文件", "work", "document"),
            ("/vol1/1000/媒体库", "media", "media")]:
        ns = argparse.Namespace(root=root, library="", kind="document")
        lib, kind = CUR._resolve_library(ns)
        assert lib and lib["id"] == want_id and kind == want_kind, \
            f"{root} 应解析为 {want_id}/{want_kind}，得 {lib}/{kind}"
        print(f"  解析 ✓ {root} → {want_id}/{want_kind}")
    print("PASS：排除断言全部通过")
    return True


if __name__ == "__main__":
    run()
