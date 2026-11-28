#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""remaining-4796 任务 1：把 72-85 带 ids 按序号 %3 交错分流（上次同口径）。
方舟 2/3，阿里 1/3，无重叠。
同时写 5 行无法定类目待核验清单（任务 3：150199/150202/150207/150210/150220，
ark 3 + ali 2）。
用法：docker exec nas-curator python3 /data/remaining4796_split.py
"""
import json

IDS = '/data/remaining4796_72_85_ids.txt'
ids = [int(x) for x in open(IDS, encoding='utf-8').read().split() if x.strip()]
ids = sorted(set(ids))
ark = [r for i, r in enumerate(ids) if i % 3 != 2]
ali = [r for i, r in enumerate(ids) if i % 3 == 2]
assert len(ark) + len(ali) == len(ids) and not set(ark) & set(ali)
with open('/data/remaining4796_72_85_ark.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(str(x) for x in ark) + '\n')
with open('/data/remaining4796_72_85_ali.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(str(x) for x in ali) + '\n')
print('total=%d ark=%d ali=%d' % (len(ids), len(ark), len(ali)))
# 任务 3 待核验 5 行
undet5 = [150199, 150202, 150207, 150210, 150220]
with open('/data/remaining4796_undet5_ark.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(str(x) for x in undet5[:3]) + '\n')
with open('/data/remaining4796_undet5_ali.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(str(x) for x in undet5[3:]) + '\n')
print('undet5 ark=%s ali=%s' % (undet5[:3], undet5[3:]))
