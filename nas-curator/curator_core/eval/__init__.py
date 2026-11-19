#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评测脚本（从 workfiles-curator scripts/eval/ 原样拷入，仅把
`import curator` / `from llmkit import` 改为包内导入）。

注意：这批脚本依赖分析期才有的数据文件（cache/audit_rows.json、
cache/samples_41.json、rules_v7.json 等），本包只带 rules_v8.json；
无数据时脚本会在读数处报错，属预期——回归时需先备好数据。
"""
