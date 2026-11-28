#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性启动器：从 stdin 读一行阿里云 Key（不落盘、不打印），
以环境变量传给前台的 remaining4796_rescue.py 后退出。
Key 只存在于子进程内存环境变量中。"""
import os, subprocess, sys

key = sys.stdin.readline().strip()
if not key:
    print("FATAL: no key on stdin", flush=True)
    sys.exit(2)
env = dict(os.environ)
env["ALIYUN_API_KEY"] = key
p = subprocess.run(["python3", "/data/remaining4796_rescue.py"], env=env,
                   stdin=subprocess.DEVNULL)
print("rescue exit=%s" % p.returncode, flush=True)
sys.exit(p.returncode)
