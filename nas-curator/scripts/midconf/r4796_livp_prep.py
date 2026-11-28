#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796 livp VLM 准备（容器内）：DB 备份＋旧 VLM 备份清空＋分流。
分流：47 行按序号 mod 3 交错，Ark 2/3、阿里 1/3。
用法：docker exec nas-curator python3 /data/r4796_livp_prep.py
"""
import json, os, sqlite3, time

DB = '/data/nas-curator.db'


def main():
    db = sqlite3.connect(DB)
    bak = '/data/nas-curator.db.bak-%s-pre-r4796-livp-vlm' % time.strftime('%Y%m%d-%H%M%S')
    with sqlite3.connect(bak) as dst:
        db.backup(dst)
    print('db backup:', bak, os.path.getsize(bak))
    db.close()


if __name__ == '__main__':
    main()
