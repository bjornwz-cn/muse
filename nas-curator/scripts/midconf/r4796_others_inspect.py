#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796 任务 步骤 4A：其他文件逐个核查（容器内，只读）。
对 /data/r4796_others.tsv 的行（跳过已非 pending 的行）：
读文件头/magic；zip（含 .livp）列出名录；文本类取前 300 字摘录；
.eml 解析发件/主题/日期与正文摘录；其余取可打印字符串样本。
输出 /data/r4796_others_inspect.tsv 与 /data/r4796_others_inspect.json。
不写库、不删文件、不解压到磁盘。
用法：docker exec nas-curator python3 /data/r4796_others_inspect.py
"""
import csv, email, json, os, re, sqlite3, zipfile
from email import policy

LIST = '/data/r4796_others.tsv'
OUT_TSV = '/data/r4796_others_inspect.tsv'
OUT_JSON = '/data/r4796_others_inspect.json'
ENC = b'__CLOUDSYNC_ENC__'
DB = '/data/nas-curator.db'


def clean(s, n=300):
    s = (s or '').replace('\t', ' ').replace('\n', ' ').replace('\r', ' ')
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:n]


def strings_sample(b, limit=8, minlen=6):
    parts = re.findall(rb'[\x20-\x7e]{%d,}' % minlen, b)
    out = []
    for p in parts:
        try:
            t = p.decode('ascii')
        except Exception:
            continue
        if t not in out:
            out.append(t)
        if len(out) >= limit:
            break
    return out


def main():
    db = sqlite3.connect(DB)
    rows = []
    with open(LIST, encoding='utf-8') as f:
        for r in csv.DictReader(f, delimiter='\t'):
            rows.append(r)
    print('list rows: %d' % len(rows), flush=True)
    out = []
    for r in rows:
        rid = int(r['rq_id'])
        src = r['src']
        prow = db.execute("SELECT status, payload FROM review_queue WHERE id=?", (rid,)).fetchone()
        status = prow[0] if prow else 'gone'
        d = json.loads(prow[1]) if prow else {}
        rec = {'rq_id': rid, 'src': src, 'ext': r['ext'], 'status': status,
               'size': r['size'], 'exists': False, 'is_dir': False,
               'head_hex': '', 'is_enc': 0, 'kind': '', 'detail': '',
               'zip_names': '', 'zip_n': 0, 'text_excerpt': '',
               'old_category': d.get('category') or '',
               'old_conf': d.get('confidence'),
               'old_vlm_cat': d.get('vlm_category') or '',
               'old_vlm_conf': d.get('vlm_confidence'),
               'doc_party': d.get('doc_party') or '',
               'old_reason': clean(str(d.get('reason') or d.get('vlm_reason') or ''), 120)}
        if status != 'pending':
            rec['detail'] = 'not pending anymore: ' + status
            out.append(rec)
            continue
        if os.path.isdir(src):
            rec['is_dir'] = True
            rec['exists'] = True
            rec['kind'] = 'directory'
            rec['detail'] = 'src is a directory, not a file'
            out.append(rec)
            continue
        try:
            st = os.stat(src)
            rec['exists'] = True
            rec['size'] = st.st_size
            with open(src, 'rb') as fh:
                head = fh.read(4096)
                fh.seek(0)
                first512 = fh.read(512)
        except FileNotFoundError:
            rec['kind'] = 'missing'
            out.append(rec)
            continue
        except Exception as e:
            rec['kind'] = 'read_error:' + type(e).__name__
            out.append(rec)
            continue
        rec['head_hex'] = head[:32].hex()
        rec['is_enc'] = int(head[:17] == ENC)
        ext = r['ext']
        if rec['is_enc']:
            rec['kind'] = 'CLOUDSYNC_ENC'
            rec['detail'] = 'encrypted blob, unreadable'
        elif head.startswith(b'PK\x03\x04'):
            rec['kind'] = 'zip'
            try:
                z = zipfile.ZipFile(src)
                names = z.namelist()
                rec['zip_n'] = len(names)
                rec['zip_names'] = ' | '.join(names[:30])
                infos = [(i.filename, i.file_size) for i in z.infolist()[:30]]
                rec['detail'] = 'zip entries=%d total_uncomp=%d; %s' % (
                    len(names), sum(i.file_size for i in z.infolist()),
                    '; '.join('%s(%d)' % (n, s) for n, s in infos[:12]))
            except Exception as e:
                rec['detail'] = 'zip open failed: %s: %s' % (type(e).__name__, str(e)[:100])
        elif head.startswith(b'Rar!'):
            rec['kind'] = 'rar'
            rec['detail'] = 'rar archive (no unrar in container); strings: %s' % (
                '; '.join(strings_sample(head, 6)))
        elif ext == '.eml':
            rec['kind'] = 'eml'
            try:
                with open(src, 'rb') as fh:
                    msg = email.message_from_binary_file(fh, policy=policy.default)
                subj = str(msg.get('Subject', ''))
                frm = str(msg.get('From', ''))
                dt = str(msg.get('Date', ''))
                body = ''
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == 'text/plain':
                            try:
                                body = part.get_content()
                            except Exception:
                                pass
                            break
                else:
                    try:
                        body = msg.get_content()
                    except Exception:
                        body = ''
                rec['detail'] = 'from=%s subject=%s date=%s' % (clean(frm, 80), clean(subj, 100), clean(dt, 40))
                rec['text_excerpt'] = clean(str(body), 300)
            except Exception as e:
                rec['detail'] = 'eml parse failed: %s: %s' % (type(e).__name__, str(e)[:100])
        elif ext in ('.py', '.json', '.bat', '.url', '.rtf', '.txt', '.md'):
            rec['kind'] = 'text'
            try:
                rec['text_excerpt'] = clean(head.decode('utf-8', errors='replace'), 300)
            except Exception:
                rec['text_excerpt'] = ''
            rec['detail'] = 'readable text, %d bytes' % len(head)
        elif ext == '.pag':
            rec['kind'] = 'pag'
            strs = strings_sample(head, 10)
            rec['detail'] = 'synology cloudsync state file? strings: %s' % ('; '.join(strs) if strs else '(none)')
        elif ext == '.exe':
            rec['kind'] = 'exe'
            rec['detail'] = 'MZ executable; strings: %s' % ('; '.join(strings_sample(head, 8)))
        elif ext == '.dwg':
            rec['kind'] = 'dwg'
            rec['detail'] = 'dwg head: %s' % head[:6].decode('ascii', errors='replace')
        elif ext == '.cr2':
            rec['kind'] = 'cr2'
            rec['detail'] = 'canon raw; tiff head'
        elif ext == '.mpg':
            rec['kind'] = 'video-mpg'
            rec['detail'] = 'mpeg video file'
        elif ext in ('.zap', '.bolt'):
            rec['kind'] = ext[1:]
            rec['detail'] = 'strings: %s' % ('; '.join(strings_sample(head, 8)))
        elif ext == '':
            rec['kind'] = 'noext'
            try:
                rec['text_excerpt'] = clean(head.decode('utf-8', errors='replace'), 200)
            except Exception:
                pass
            rec['detail'] = 'no extension; strings: %s' % ('; '.join(strings_sample(head, 6)))
        else:
            rec['kind'] = 'other-binary'
            rec['detail'] = 'strings: %s' % ('; '.join(strings_sample(head, 6)))
        out.append(rec)
    with open(OUT_TSV, 'w', encoding='utf-8') as f:
        f.write('rq_id\text\tsize\tstatus\tkind\tis_enc\tzip_n\told_category\told_conf\tdoc_party\tsrc\tdetail\ttext_excerpt\n')
        for r in out:
            f.write('%d\t%s\t%s\t%s\t%s\t%d\t%d\t%s\t%s\t%s\t%s\t%s\t%s\n' % (
                r['rq_id'], r['ext'], r['size'], r['status'], r['kind'], r['is_enc'],
                r['zip_n'], clean(str(r['old_category']), 40), r['old_conf'],
                clean(r['doc_party'], 20), clean(r['src'], 120),
                clean(r['detail'], 400), clean(r['text_excerpt'], 300)))
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    from collections import Counter
    print('SUMMARY n=%d kinds=%s' % (len(out), dict(Counter(r['kind'] for r in out))))
    print('not pending: %d missing: %d dirs: %d enc: %d' % (
        sum(1 for r in out if r['status'] != 'pending'),
        sum(1 for r in out if r['kind'] == 'missing'),
        sum(1 for r in out if r['is_dir']),
        sum(1 for r in out if r['is_enc'])))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
