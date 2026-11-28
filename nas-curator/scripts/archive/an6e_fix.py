#!/usr/bin/env python3
# an6e: fix 3 misclassifications in an6_classify.jsonl
import json
from collections import Counter

rows = [json.loads(l) for l in open('/data/an6_classify.jsonl')]
fixes = {
    138894: ('B', '', '文件名是佛经名，内容却是 Java 示例代码，名实不符、用途不明'),
    138898: ('B', '', '文件名是佛经名，内容却是 JMS/Java 示例代码，名实不符、用途不明'),
    139398: ('C', '家庭文件/', '王子婴个人信息（姓名/性别/出生日期/身份证号/学校），家庭文件'),
}
for r in rows:
    if r['id'] in fixes:
        r['cls'], r['dest'], r['note'] = fixes[r['id']]
with open('/data/an6_classify.jsonl', 'w') as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + '\n')
print('CLASS:', Counter(r['cls'] for r in rows))
print('C by dest:', Counter(r['dest'] for r in rows if r['cls'] == 'C'))
print('B by ext:', Counter(r['ext'] for r in rows if r['cls'] == 'B'))
with open('/data/an6_summary.txt', 'w') as f:
    for cls in ('A', 'B', 'C'):
        n = sum(1 for r in rows if r['cls'] == cls)
        f.write('\n===== %s 类 (%d) =====\n' % (cls, n))
        for r in rows:
            if r['cls'] != cls:
                continue
            f.write('%s [%s] %s | %s | %s\n' % (r['id'], r['ext'], r['name'], r['dest'], r['note']))
print('fixed and saved')
