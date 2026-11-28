#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796 livp 分流（容器内）：47 行按序号 mod 3，Ark 2/3、阿里 1/3。"""
import csv

ids = []
with open('/data/r4796_livp_vlm.tsv', encoding='utf-8') as f:
    for r in csv.DictReader(f, delimiter='\t'):
        ids.append(int(r['rq_id']))
ark = [r for i, r in enumerate(ids) if i % 3 != 2]
ali = [r for i, r in enumerate(ids) if i % 3 == 2]
print('livp:', len(ids), 'ark:', len(ark), 'ali:', len(ali))
open('/data/r4796_livp_ark_rqids.txt', 'w').write(','.join(map(str, ark)))
open('/data/r4796_livp_ali_rqids.txt', 'w').write(','.join(map(str, ali)))
print('ARK:', ','.join(map(str, ark)))
print('ALI:', ','.join(map(str, ali)))
