"""api 包：NAS Curator P2 Web 后端（FastAPI）。

只做 HTTP 层：路由 + 入参校验 + 调 P1 引擎（curator.py / curator_core /
media_core）+ common.db。整理任务一律先 plan 再 apply，后台线程跑。
"""
from __future__ import annotations

import os
import sys

# 保证从任意工作目录 import api 时，项目根目录都在 sys.path 上
#（curator.py / common / curator_core 都按项目根目录 import）。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

__all__ = []
