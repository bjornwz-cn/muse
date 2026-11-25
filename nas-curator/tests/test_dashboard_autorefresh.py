#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0.2.2：总览页任务卡片自动刷新。

背景（王震 2026-11-26 反馈）：收件箱里的文件已经处理了，软件上的任务卡片
没有自动刷新。根因在 web/templates/index.html：load()（库卡片/待复核数/
今日新增/重复可回收/最近任务）只在页面加载时跑一次，只有“进行中任务”区
有 5 秒轮询；后台处理完后进行中的卡片消失了，其他卡片还停在旧数。
后端 /api/dashboard 每次都实时查库，没有问题。

验证方式：Node 桩环境跑模板内联脚本（dashboard_autorefresh_harness.js，
与 test_template_js.py 同口径：只用项目内文件与本地 node，不碰 NAS）：
首轮 /api/dashboard 返回旧数，之后返回新数；触发 5 秒 interval 回调后
卡片必须变成新数；数据未变再触发一次时 DOM 不得重写（防闪烁）。
"""
import json
import os
import shutil
import subprocess

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(PROJECT_ROOT, "tests", "dashboard_autorefresh_harness.js")
TEMPLATE = os.path.join(PROJECT_ROOT, "web", "templates", "index.html")


def _run_harness():
    node = shutil.which("node")
    if not node:
        pytest.skip("node 未安装，跳过总览自动刷新检查")
    r = subprocess.run([node, HARNESS, TEMPLATE],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    assert r.returncode == 0, \
        f"刷新验证桩运行失败：{r.stderr.decode('utf-8', 'replace')}"
    return json.loads(r.stdout.decode("utf-8"))


def test_dashboard_cards_refresh_on_interval():
    out = _run_harness()
    assert out["intervals"], "总览页没有注册任何轮询 interval"
    assert 5000 in out["intervals"], "总览页轮询间隔不是 5 秒"
    # 首轮是旧数
    assert out["before"]["statReview"] == "275"
    assert out["before"]["dashCalls"] == 1
    # 一次轮询后，后台处理完的新数必须出现在卡片上（不再是旧数）
    assert out["after"]["statReview"] == "65", \
        "5 秒轮询后复核待处理数仍是旧数，任务卡片未自动刷新"
    assert "57" in out["after"]["libCards"], "库卡片文件数未更新为新数"
    assert "done" in out["after"]["jobsBody"], "最近任务表未更新为已完成"
    assert out["after"]["badge"] == "65", "顶部导航待复核徽标未随轮询更新"
    assert out["after"]["dashCalls"] >= 2, "轮询没有重新请求 /api/dashboard"
    # 数据未变再轮询一次：仍请求，但 DOM 不重写（防闪烁）
    assert out["after2"]["dashCalls"] > out["after"]["dashCalls"]
    assert out["after2"]["libCards"] == out["after"]["libCards"]
    assert out["after2"]["jobsBody"] == out["after"]["jobsBody"]
    assert out["after2"]["statReview"] == out["after"]["statReview"]
