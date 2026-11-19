#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""common.rollback —— 回滚：按 operations 逆序把文件移回原位置。

逻辑与 workfiles-curator 原版 cmd_rollback 一致：
- 映射表格式：TSV，每行 `原路径 \\t 新路径 \\t sha256 \\t 时间`（第 3 列可为空）。
- 逆序执行；目标（新位置）不存在记缺失；原位置已有文件时加 `__n` 后缀，绝不覆盖。
- 回滚后清理遗留的空类目目录，但严格限制在映射表所属根目录内，绝不越界。
- 另提供 build_rollback_script：生成与原版同格式的 bash 回滚脚本（mv -n）。
"""
from __future__ import annotations

import os
import shutil


def _unique_final(path):
    """原位置已有文件时加 __n 后缀，绝不覆盖。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{stem}__{i}{ext}"):
        i += 1
    return f"{stem}__{i}{ext}"


def _shell_quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


def build_rollback_script(operations, out_path):
    """operations: list of dicts with src_path / dst_path。
    生成 bash 脚本（与原版同格式）：逆序 `mkdir -p 原目录 && mv -n 新位置 原位置`。"""
    lines = ["#!/usr/bin/env bash",
             "# 自动生成：逆序移回原位置（含改名还原）",
             "set -uo pipefail"]
    for op in reversed(operations):
        src, dst = op["src_path"], op["dst_path"]
        lines.append(f'mkdir -p {_shell_quote(os.path.dirname(src))}'
                     f' && mv -n {_shell_quote(dst)} {_shell_quote(src)}')
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(out_path, 0o755)
    return out_path


def rollback_pairs(pairs, dry_run=False):
    """pairs: [(原路径, 新路径), ...]。逆序移回。返回 (moved, missing, failed)。"""
    moved, missing, failed = 0, [], []
    for src, dst in reversed(pairs):
        if not os.path.exists(dst):
            missing.append(dst)
            continue
        if dry_run:
            moved += 1
            continue
        try:
            os.makedirs(os.path.dirname(src), exist_ok=True)
            final = _unique_final(src)
            shutil.move(dst, final)
            moved += 1
        except OSError as e:
            failed.append((dst, str(e)))
    if not dry_run and pairs:
        _cleanup_empty_dirs(pairs)
    return moved, missing, failed


def _cleanup_empty_dirs(pairs):
    """清理遗留空类目目录，严格限制在映射表所属根目录内。"""
    try:
        root = os.path.commonpath([os.path.dirname(t) for _, t in pairs])
    except ValueError:
        return
    for _, target in pairs:
        d = os.path.dirname(target)
        while root and d != root and d.startswith(root + os.sep):
            try:
                os.rmdir(d)
            except OSError:
                break
            d = os.path.dirname(d)


def rollback_from_map(map_path, dry_run=False):
    """从 TSV 映射表回滚（兼容原版 rollback --map）。"""
    pairs = []
    with open(map_path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2 and parts[0] and parts[1]:
                pairs.append((parts[0], parts[1]))
    moved, missing, failed = rollback_pairs(pairs, dry_run=dry_run)
    return {"moved": moved, "missing": missing, "failed": failed}


def rollback_from_operations(conn, plan_id, dry_run=False, mark=True):
    """从 DB 的 operations 表回滚某次 plan；成功移回的逐条写 rolled_back_at。"""
    from . import db as _db
    ops = _db.list_operations(conn, plan_id)
    pairs = [(o["src_path"], o["dst_path"]) for o in ops]
    moved, missing, failed = rollback_pairs(pairs, dry_run=dry_run)
    if mark and not dry_run:
        moved_dsts = set()
        # 只标记实际移回成功的条目：按逆序逐条比对（dst 已不存在即已移回）
        for o in ops:
            if not os.path.exists(o["dst_path"]) and os.path.exists(o["src_path"]):
                moved_dsts.add(o["id"])
        for op_id in moved_dsts:
            _db.mark_rolled_back(conn, op_id)
    return {"moved": moved, "missing": missing, "failed": failed,
            "plan_id": plan_id}
