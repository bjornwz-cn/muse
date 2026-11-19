#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 媒体抽样（在 NAS 上运行，需 SigLIP + ChineseCLIP 模型缓存）。

流程（调用方执行，不在本机跑）：
 1. 本机：经 nas-ssh.sh 列 /vol1/1000/媒体库/_整理 下的媒体文件，抽 30 条写 sample_list.txt
 2. 把 ~/workspace/projects/nas-curator/ 拷到 NAS /tmp/nas-curator-p1-test/（临时，测完即删）
 3. NAS 上：SP=<marvis-python 的 site-packages>；
    LD_LIBRARY_PATH=$SP/opencv_python_headless.libs:$SP/opencv_contrib_python.libs:$SP/torch/lib
    HF_HUB_OFFLINE=1 <marvis-python> /tmp/nas-curator-p1-test/tests/test_media_sample_nas.py \
        --list /tmp/sample_list.txt --config /tmp/nas-curator-p1-test/media_core/config.json
    输出 JSON 到 stdout，由调用方保存并人工抽查 30 条分类是否合理。
    注：LD_LIBRARY_PATH 不可省——marvis python 的 cv2 自身也要 libopenblasp，
    .libs 不在默认 so 搜索路径里（2026-11-19 实测；ensure_cv2_first 内有
    ctypes 预加载兜底，但启动时带上环境变量最稳）。
只读：只分类、不移动、不写 NAS 文件（除 /tmp 临时目录）。
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, PROJECT_ROOT)

# 先 import cv2（其 .libs 含 OpenBLAS），再由 media_core 加载 torch（见 media_core 说明）。
try:
    import cv2  # noqa: F401
except Exception as e:
    print(f"WARN: cv2 导入失败（{e}），torch 加载可能随后失败", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", required=True, help="待测文件列表（每行一个绝对路径）")
    ap.add_argument("--config", default=os.path.join(PROJECT_ROOT, "media_core", "config.json"))
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    # 模型缓存固定用 NAS 现有位置（不下载、不改配置）。
    cfg.setdefault("model_dir",
                   "/vol2/@appdata/marvis-app/data/home/agent-memory/media-curator/models")
    paths = [l.strip() for l in open(args.list, encoding="utf-8") if l.strip()][:args.limit]

    from media_core import engine as ME
    from media_core import Classifier
    cls = Classifier(cfg)
    items, stats = ME.plan_media_files(paths, cfg, classifier=cls,
                                       library_root="/vol1/1000/媒体库")
    out = []
    for it in items:
        out.append({"src": it["src"], "category": it.get("category"),
                    "confidence": it.get("confidence"), "reason": it.get("reason"),
                    "evidence": it.get("evidence"), "action": it.get("action"),
                    "review_kind": it.get("review_kind"), "tags": it.get("tags")})
    sys.stdout.write(json.dumps({"n": len(out), "stats": stats, "items": out},
                              ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
