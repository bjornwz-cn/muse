#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复核队列深挖（只读，跑在容器内，读 /tmp/rq_pending.json）。"""
import collections
import json
import os
import re

rows = json.load(open("/tmp/rq_pending.json", encoding="utf-8"))

def top_of(src):
    if not src:
        return "unknown"
    if src.startswith("/vol1/1000/工作文件/"):
        return "work"
    if src.startswith("/vol1/1000/家庭文件/"):
        return "family"
    if src.startswith("/vol1/1000/媒体库/"):
        return "media"
    if src.startswith("/vol1/1000/收件箱/"):
        return "inbox"
    if src.startswith("/vol1/1000/下载/Wangzhen-Mac/"):
        return "mac-backup"
    if src.startswith("/vol1/1000/"):
        return "vol1-other"
    return "other"

def person_of(src):
    m = re.match(r"/vol1/1000/家庭文件/([^/]+)/", src or "")
    return m.group(1) if m else ""

print("== mac-backup dup: evidence 指向 ==")
c = collections.Counter()
examples = {}
for r in rows:
    if r["kind"] != "dup":
        continue
    p = json.loads(r["payload"])
    ev = p.get("evidence") or ""
    src = p.get("src") or ""
    if top_of(src) != "mac-backup":
        continue
    key = (top_of(ev), "same-dir" if os.path.dirname(ev) == os.path.dirname(src) else "other-dir")
    c[key] += 1
    examples.setdefault(key, (src, ev))
for k, n in sorted(c.items()):
    print(f"  {k}: {n}  例：{examples[k]}")

print("== family dup: evidence 指向 ==")
c = collections.Counter()
for r in rows:
    if r["kind"] != "dup":
        continue
    p = json.loads(r["payload"])
    ev = p.get("evidence") or ""
    src = p.get("src") or ""
    if top_of(src) != "family":
        continue
    key = (top_of(ev), "same-dir" if os.path.dirname(ev) == os.path.dirname(src) else "other-dir")
    c[key] += 1
for k, n in sorted(c.items()):
    print(f"  {k}: {n}")

print("== family low_conf: src 路径形态 ==")
c = collections.Counter()
for r in rows:
    if r["kind"] != "low_conf":
        continue
    p = json.loads(r["payload"])
    src = p.get("src") or ""
    if top_of(src) != "family":
        continue
    rel = src[len("/vol1/1000/家庭文件/"):]
    parts = [x for x in rel.split("/") if x]
    if len(parts) <= 1:
        shape = "家庭文件/根（无子目录）"
    elif len(parts) == 2:
        shape = "家庭文件/<人>/<文件>（人在根下，无内容子目录）"
    else:
        shape = "家庭文件/<人>/<内容子目录>/…"
    c[(shape, person_of(src) or "?")] += 1
for (s, person), n in sorted(c.items()):
    print(f"  {person:8s} {s}: {n}")

print("== family low_conf: 内容子目录分布（第 3 段，top 20） ==")
c = collections.Counter()
for r in rows:
    if r["kind"] != "low_conf":
        continue
    p = json.loads(r["payload"])
    src = p.get("src") or ""
    if top_of(src) != "family":
        continue
    rel = src[len("/vol1/1000/家庭文件/"):]
    parts = [x for x in rel.split("/") if x]
    if len(parts) >= 3:
        c[parts[1]] += 1
for k, n in c.most_common(20):
    print(f"  {k}: {n}")

print("== family low_conf: 扩展名分布 top 10 ==")
c = collections.Counter()
for r in rows:
    if r["kind"] != "low_conf":
        continue
    p = json.loads(r["payload"])
    src = p.get("src") or ""
    if top_of(src) != "family":
        continue
    c[os.path.splitext(src)[1].lower() or "(无)"] += 1
for k, n in c.most_common(10):
    print(f"  {k}: {n}")

print("== mac-backup low_conf/blur/ambiguous: src 形态 ==")
c = collections.Counter()
for r in rows:
    p = json.loads(r["payload"])
    src = p.get("src") or ""
    if top_of(src) != "mac-backup":
        continue
    rel = src[len("/vol1/1000/下载/Wangzhen-Mac/"):]
    parts = [x for x in rel.split("/") if x]
    c[(r["kind"], parts[0] if parts else "?")] += 1
for (k, first), n in sorted(c.items()):
    print(f"  {k:12s} 首段={first}: {n}")

print("== plan 分布（pending） ==")
c = collections.Counter((r["plan_id"], r["kind"]) for r in rows)
for (pid, k), n in sorted(c.items()):
    print(f"  plan {pid} {k}: {n}")

print("== low_conf reason 分布 top 10 ==")
c = collections.Counter()
for r in rows:
    if r["kind"] != "low_conf":
        continue
    p = json.loads(r["payload"])
    c[p.get("reason") or ""] += 1
for k, n in c.most_common(10):
    print(f"  {k}: {n}")

print("== dup reason 分布 ==")
c = collections.Counter()
for r in rows:
    if r["kind"] != "dup":
        continue
    p = json.loads(r["payload"])
    c[p.get("reason") or ""] += 1
for k, n in c.most_common(10):
    print(f"  {k}: {n}")
