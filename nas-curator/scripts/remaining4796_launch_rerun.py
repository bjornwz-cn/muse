#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性启动器：从 stdin 读一行阿里云 Key（不落盘、不打印），
以环境变量传给 detached 的 remaining4796_rerun.py 子进程后即退出。
Key 只存在于子进程内存环境变量中。"""
import os, subprocess, sys

key = sys.stdin.readline().strip()
if not key:
    print("FATAL: no key on stdin", flush=True)
    sys.exit(2)
env = dict(os.environ)
env["ALIYUN_API_KEY"] = key
log = open("/data/remaining4796_rerun.log", "ab")
p = subprocess.Popen(
    ["python3", "/data/remaining4796_rerun.py", "--workers", "4"],
    env=env, stdout=log, stderr=subprocess.STDOUT,
    start_new_session=True, stdin=subprocess.DEVNULL)
print("launched pid=%s" % p.pid, flush=True)
