#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P4：plan --deep 深挖模式测试。

默认 auto 模式只下钻透明目录（收件箱/待处理等），对已整理的库覆盖不足
（工作文件 14,427 文件只产出 3 项）；--deep 下钻全部子目录（max_depth
2→5），修库全量覆盖用。只用 tmp 目录与临时 DB，不碰 NAS。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import curator as CUR  # noqa: E402
from common import db as DB  # noqa: E402

RULES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "curator_core", "rules.baijing-supply-v8.json")


def _mk_tree(root):
    os.makedirs(os.path.join(root, "待处理"), exist_ok=True)
    os.makedirs(os.path.join(root, "项目A", "子目录"), exist_ok=True)
    files = {
        "顶级合同.txt": "产品销售合同 甲方北京红橙天天健康科技有限公司",
        os.path.join("待处理", "报价单.txt"): "产品销售合同报价清单",
        os.path.join("项目A", "深层文件.txt"): "产品销售合同 补充协议",
        os.path.join("项目A", "子目录", "更深文件.txt"): "产品销售合同 技术附件",
    }
    for rel, text in files.items():
        with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
            f.write(text)
    return len(files)


def _run_plan(root, db, deep):
    argv = ["--db", db, "plan", "--root", root, "--rules", RULES, "--no-ocr"]
    if deep:
        argv.append("--deep")
    ns = CUR.build_parser().parse_args(argv)
    assert CUR.cmd_plan(ns) == 0


def test_plan_deep_covers_nested_dirs(tmp_path):
    root = str(tmp_path / "lib")
    os.makedirs(root)
    n_files = _mk_tree(root)
    assert n_files == 4

    db_auto = str(tmp_path / "auto.db")
    _run_plan(root, db_auto, deep=False)
    conn = DB.connect(db_auto)
    n_auto = conn.execute("SELECT COUNT(*) FROM plan_items").fetchone()[0]
    conn.close()

    db_deep = str(tmp_path / "deep.db")
    _run_plan(root, db_deep, deep=True)
    conn = DB.connect(db_deep)
    n_deep = conn.execute("SELECT COUNT(*) FROM plan_items").fetchone()[0]
    deep_srcs = [r[0] for r in conn.execute(
        "SELECT src_path FROM plan_items").fetchall()]
    conn.close()

    assert n_auto == 2, n_auto  # 顶级 + 透明目录
    assert n_deep == 4, n_deep  # 全部文件
    assert any("更深文件.txt" in s for s in deep_srcs)


def test_load_plan_rules_selects_by_library(tmp_path):
    """P4：--library family 的计划须用家庭规则，不是工作库 v8 规则。

    此前 _load_plan_rules 一律返回 v8，家庭文件会被按工作类目（01-合同与协议
    等）分类。只测规则选择本身，不跑完整 plan。
    """
    import argparse

    # 家庭库：按 root 匹配到 family，rules 字段为 family-default
    ns = argparse.Namespace(rules="", library="family",
                            root="/vol1/1000/家庭文件/刘音", kind="document")
    rules = CUR._load_plan_rules(ns)
    assert rules is not None
    paths = [c["path"] for c in rules.get("categories", [])]
    assert "证件与户籍" in paths
    assert not any(p.startswith("01-") for p in paths)

    # 工作库：仍用 v8
    ns = argparse.Namespace(rules="", library="work",
                            root="/vol1/1000/工作文件/1-王震", kind="document")
    rules = CUR._load_plan_rules(ns)
    paths = [c["path"] for c in rules.get("categories", [])]
    assert any(p.startswith("01-") for p in paths)

    # 显式 --rules 优先于按库选择
    ns = argparse.Namespace(rules=RULES, library="family",
                            root="/vol1/1000/家庭文件/刘音", kind="document")
    rules = CUR._load_plan_rules(ns)
    paths = [c["path"] for c in rules.get("categories", [])]
    assert any(p.startswith("01-") for p in paths)
