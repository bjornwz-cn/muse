#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""an6d: classify the 268 review files into A/B/C.
A = damaged/unopenable (delete) -- expected 0 after re-verification.
B = openable but content unidentifiable (keep, list with reason).
C = openable and content identifiable (keep, list with suggested destination).
Corrections to an6_check verdicts (verified by hand):
- 137904, 137958 (.xls SpreadsheetML): openable, huge sales data -> C
- 141865, 142045 (.mov), 150079 (.mp4): cv2 opens, real content -> yes, but
  content unknown -> B
- 138707 (._ AppleDouble): valid macOS metadata, not a spreadsheet -> special/B
Output: /data/an6_classify.jsonl + /data/an6_summary.txt
"""
import json, os, re

CHECK = '/data/an6_check.jsonl'
PENDING = '/data/an6_pending.json'

CORRECT = {
    137904: ('yes', 'SpreadsheetML XML, 102666 rows, sales/order data (分部/场所/库位/定单号...)'),
    137958: ('yes', 'SpreadsheetML XML, 69023 rows, sales/order data (脊柱通用)'),
    141865: ('yes', 'cv2 opens 1440x1920 37f, brightness ~148-93, real content'),
    142045: ('yes', 'cv2 opens 1920x1080 2068f, brightness ~154, real content'),
    150079: ('yes', 'cv2 opens 1284x1712 199f, brightness ~141, real content'),
    138707: ('unknown', 'AppleDouble macOS metadata (00 05 16 07), not a spreadsheet'),
}

# filenames whose content mismatches the name (Buddhist/Taoist/literary names, code content)
MISMATCH_TXT = re.compile(r'(佛说|陀罗尼|经\.txt|道经|太上|上清|黄帝|文殊|洛阳记|谕对录|蚕书|述煮茶|礼法华)')

def suggest(name, ext, snippet, cat):
    """Return (suggested_dest, content_note) for class C, or None for B."""
    n = name
    s = snippet or ''
    # credentials
    if 'Access Key' in s or 'Secret Access Key' in s:
        return ('家庭文件/王震/（凭据）', '含火山引擎 ArkClaw 账号凭据信息')
    # family / kids
    if any(k in n for k in ['王子婴', '口试题目', '振兴二校']):
        return ('家庭文件/', '孩子学校/个人信息')
    # Liaoning companies
    if any(k in n for k in ['哈尔滨', '沈阳', '凌源', '辽吉', '辽宁']):
        return ('工作文件/4-辽宁器械/', '辽宁相关公司/业务文件')
    # personal dev / Agent / WALL-E / mfkey32
    if any(k in n for k in ['WALL-E', 'Bambu', 'mfkey', 'Agent Team', 'OpenClaw', 'OPENCLAW',
                             'runxin-workbench', 'trails-', 'heatmap', 'NodeEx', 'pm3_', 'text2bin']):
        return ('工作文件/1-王震/', '个人开发/Agent/3D打印相关')
    # Curator working files
    if n.startswith('_删除') or n.startswith('_执行日志') or n.startswith('_映射表'):
        return ('工作文件/1-王震/（Curator 工作文件，可归档）', '文件管家运行日志/映射表')
    # work emails
    if ext == '.eml':
        return ('工作文件/2-上海润心/', '公司邮件：' + (s[:40] if s else n[:40]))
    # default: Shanghai Runxin business
    return ('工作文件/2-上海润心/', '公司业务文件')

def main():
    rows = [json.loads(l) for l in open(CHECK)]
    pend = {it['id']: it for it in json.load(open(PENDING))}
    out = []
    for r in rows:
        rid = r['id']
        name = r['name']
        ext = r['ext']
        verdict, detail = r['verdict'], r['detail']
        snippet = r.get('snippet') or ''
        if rid in CORRECT:
            verdict, detail = CORRECT[rid][0], CORRECT[rid][1]
        # --- class A: damaged ---
        if verdict == 'no':
            cls, dest, note = 'A', '', '损坏无法打开：' + detail
        # --- special / unknown ---
        elif verdict == 'unknown':
            cls, dest, note = 'B', '', '特殊文件无法验证（不删）：' + detail
        # --- images/videos: openable but content unknown ---
        elif ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.tiff'):
            cls, dest, note = 'B', '', '照片可打开，VLM 低置信无法辨认内容'
        elif ext in ('.mov', '.mp4', '.mts', '.mpg', '.vob'):
            cls, dest, note = 'B', '', '视频可打开，VLM 低置信无法辨认内容'
        # --- mismatched txt ---
        elif ext == '.txt' and MISMATCH_TXT.search(name):
            cls, dest, note = 'B', '', '文件名是佛经/道经/古籍名，内容却是代码/示例文本，名实不符、用途不明'
        # --- zip with unverifiable content ---
        elif ext == '.zip':
            cls, dest, note = 'B', '', '压缩包可打开，但内容无法确认（信明启通文件名乱码；共聚显微影像仪为产品资料、所属方不明）'
        # --- class C ---
        else:
            dest, note = suggest(name, ext, snippet, r.get('cat') or '')
            cls = 'C'
        out.append({'id': rid, 'name': name, 'ext': ext, 'size': r['size'],
                    'cls': cls, 'dest': dest, 'note': note,
                    'detail': detail, 'snippet': snippet[:150]})
    with open('/data/an6_classify.jsonl', 'w') as f:
        for o in out:
            f.write(json.dumps(o, ensure_ascii=False) + '\n')
    from collections import Counter
    print('CLASS:', Counter(o['cls'] for o in out))
    print('B by ext:', Counter(o['ext'] for o in out if o['cls'] == 'B'))
    print('C by dest:', Counter(o['dest'] for o in out if o['cls'] == 'C'))
    # write human-readable summary
    with open('/data/an6_summary.txt', 'w') as f:
        for cls in ('A', 'B', 'C'):
            f.write(f'\n===== {cls} 类 ({sum(1 for o in out if o["cls"]==cls)}) =====\n')
            for o in out:
                if o['cls'] != cls:
                    continue
                f.write(f"{o['id']} [{o['ext']}] {o['name']} | {o['dest']} | {o['note']}\n")
    print('saved /data/an6_classify.jsonl, /data/an6_summary.txt')
    return 0

if __name__ == '__main__':
    main()
