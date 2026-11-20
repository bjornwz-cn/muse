#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0.1.7：全部模板的内联 <script> 必须能过 node --check。

背景：index.html 第 133–134 行 `var cards = ...` 重复了一行，内联脚本
解析失败，总览页 load() 从不执行、页面永久停在“加载中...”。此前各版本
验收只查六页 HTTP 200，没查 JS 解析，所以从 0.1.4 引入起一直漏网。
部署核查清单已加这一项（见 P10 报告）。

只用项目内模板与本地 node，不碰 NAS。模板内联脚本不得含 Jinja 插值
（当前 7 个模板均无；一旦有人在 <script> 里写 {{ }}，本测试会失败，
提醒把数据改走 DOM 属性或 API）。
"""
import os
import re
import shutil
import subprocess
import tempfile

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(PROJECT_ROOT, "web", "templates")

_INLINE_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def _inline_scripts(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return [m.group(1).strip() for m in _INLINE_RE.finditer(text)
            if m.group(1).strip()]


def test_all_template_inline_js_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("node 未安装，跳过模板 JS 解析检查")
    files = sorted(f for f in os.listdir(TEMPLATE_DIR) if f.endswith(".html"))
    assert files, "web/templates 下没有模板"
    checked = 0
    for name in files:
        path = os.path.join(TEMPLATE_DIR, name)
        for i, body in enumerate(_inline_scripts(path)):
            assert "{{" not in body and "{%" not in body, \
                f"{name} script#{i} 含 Jinja 插值，node 无法解析（数据改走 DOM 属性或 API）"
            with tempfile.NamedTemporaryFile("w", suffix=".js",
                                            delete=False, encoding="utf-8") as tf:
                tf.write(body)
                tmp = tf.name
            try:
                r = subprocess.run([node, "--check", tmp],
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=30)
            finally:
                os.unlink(tmp)
            assert r.returncode == 0, \
                f"{name} script#{i} 语法错误：{r.stderr.decode('utf-8', 'replace')}"
            checked += 1
    assert checked >= 1, "没有抽到任何内联脚本"
    print(f"模板 JS 解析：{len(files)} 个模板、{checked} 段内联脚本全过 node --check")
