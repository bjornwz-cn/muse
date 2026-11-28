#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""an6: check openability of all 268 files in /vol1/1000/收件箱/待复核/.
Per-file verdict: openable=yes/no/unknown + detail + content snippet.
- images: PIL full decode (load); fallback cv2; HEIC via cv2/ffmpeg fallback.
- videos: ffprobe streams + cv2 frame read; mean brightness for all-black.
- pdf: pypdf pages + text extract; fallback pdftotext.
- xlsx: openpyxl; xls/doc/ppt: olefile streams; docx/pptx: zipfile + xml text.
- zip: zipfile testzip; eml: email parser; txt/md/csv/html/json/py/bat: read text.
- exe/webloc/url/zap/bolt/osheet/.folderstructure/noext: special, do not delete.
Output: /data/an6_check.jsonl (one JSON per file).
Usage: docker exec nas-curator python3 /data/an6_check.py
"""
import json, os, subprocess, sys

REVIEW_DIR = '/vol1/1000/收件箱/待复核'
PENDING_JSON = '/data/an6_pending.json'
OUT = '/data/an6_check.jsonl'

def sh(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or '') + (r.stderr or '')
    except Exception as e:
        return -1, str(e)

def check_image(path):
    # PIL full decode
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.load()
            return ('yes', f'PIL {im.format} {im.size[0]}x{im.size[1]} mode={im.mode}', '')
    except Exception as e:
        pil_err = str(e)[:120]
    # cv2 fallback
    try:
        import cv2
        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if img is not None and img.size > 0:
            h, w = img.shape[:2]
            return ('yes', f'cv2 {w}x{h} (PIL failed: {pil_err})', '')
    except Exception as e:
        pass
    # ffmpeg fallback (covers heic/webp edge cases)
    rc, out = sh(f'ffprobe -v error -select_streams v:0 -show_entries stream=width,height,codec_name -of csv=p=0 {json.dumps(path)}')
    if rc == 0 and out.strip():
        return ('yes', f'ffprobe {out.strip()} (PIL failed: {pil_err})', '')
    return ('no', f'PIL fail: {pil_err}; cv2/ffprobe fail', '')

def check_video(path):
    rc, out = sh(f'ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,width,height,duration -show_entries format=duration -of default=noprint_wrappers=1 {json.dumps(path)}')
    if rc != 0 or not out.strip():
        return ('no', 'ffprobe no video stream', '')
    # cv2 frame read + brightness
    try:
        import cv2
        import numpy as np
        cap = cv2.VideoCapture(path)
        n = 0
        means = []
        while n < 10:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            n += 1
            means.append(float(frame.mean()))
        cap.release()
        if n == 0:
            return ('no', 'ffprobe ok but cv2 read 0 frames', '')
        avg = sum(means) / len(means)
        if avg < 1.0:
            return ('no', f'all-black video: {n} frames mean brightness {avg:.2f}', '')
        return ('yes', f'{n} frames read, mean brightness {avg:.1f}; {out.strip()[:120]}', '')
    except Exception as e:
        return ('unknown', f'ffprobe ok, cv2 err: {str(e)[:100]}', '')

def check_pdf(path):
    try:
        from pypdf import PdfReader
        r = PdfReader(path)
        n = len(r.pages)
        txt = ''
        try:
            txt = (r.pages[0].extract_text() or '')[:200] if n else ''
        except Exception:
            pass
        if n == 0:
            return ('no', 'pypdf: 0 pages', '')
        return ('yes', f'pypdf {n} pages', txt.replace('\n', ' '))
    except Exception as e:
        rc, out = sh(f'pdftotext -l 1 {json.dumps(path)} - 2>/dev/null | head -c 200')
        if rc == 0 and out.strip():
            return ('yes', 'pdftotext ok (pypdf failed)', out.strip().replace('\n', ' ')[:200])
        return ('no', f'pypdf fail: {str(e)[:120]}; pdftotext empty', '')

def check_xlsx(path):
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        sheets = wb.sheetnames
        cells = 0
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                for v in row:
                    if v is not None and str(v).strip() != '':
                        cells += 1
                        if cells >= 5:
                            break
                if cells >= 5:
                    break
            if cells >= 5:
                break
        wb.close()
        if cells == 0:
            return ('yes', f'xlsx {len(sheets)} sheets, no non-empty cells (empty workbook)', '')
        return ('yes', f'xlsx {len(sheets)} sheets, has content', '')
    except Exception as e:
        return ('no', f'openpyxl fail: {str(e)[:120]}', '')

def check_ole(path, want_stream):
    try:
        import olefile
        if not olefile.isOleFile(path):
            return ('no', 'not an OLE file', '')
        ole = olefile.OleFileIO(path)
        streams = ole.listdir(streams=True)
        names = ['/'.join(s) for s in streams]
        ole.close()
        if any(want_stream in nm for nm in names):
            return ('yes', f'OLE ok, streams: {len(names)}, has {want_stream}', '')
        return ('yes', f'OLE ok, {len(names)} streams, no {want_stream} (may be empty)', '')
    except Exception as e:
        return ('no', f'olefile fail: {str(e)[:120]}', '')

def check_zip_xml(path, xml_path, tag):
    try:
        import zipfile
        z = zipfile.ZipFile(path)
        bad = z.testzip()
        if bad is not None:
            return ('no', f'zip corrupt at {bad}', '')
        names = z.namelist()
        if xml_path not in names:
            return ('no', f'zip ok but no {xml_path}', '')
        import re
        data = z.read(xml_path).decode('utf-8', errors='ignore')
        text = re.sub(r'<[^>]+>', ' ', data)
        text = ' '.join(text.split())[:200]
        z.close()
        return ('yes', f'zip ok, {len(names)} entries', text)
    except Exception as e:
        return ('no', f'zip fail: {str(e)[:120]}', '')

def check_zip(path):
    try:
        import zipfile
        z = zipfile.ZipFile(path)
        bad = z.testzip()
        names = z.namelist()
        z.close()
        if bad is not None:
            return ('no', f'zip corrupt at {bad}', '')
        return ('yes', f'zip ok, {len(names)} entries', '')
    except Exception as e:
        return ('no', f'zip fail: {str(e)[:120]}', '')

def check_eml(path):
    try:
        import email
        from email import policy
        from email.parser import BytesParser
        with open(path, 'rb') as f:
            msg = BytesParser(policy=policy.default).parse(f)
        subj = str(msg['Subject'] or '')[:80]
        body = ''
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == 'text/plain':
                    try:
                        body = part.get_content()[:200]
                        break
                    except Exception:
                        pass
        else:
            try:
                body = str(msg.get_content())[:200]
            except Exception:
                body = ''
        return ('yes', f'eml ok subject={subj}', body.replace('\n', ' '))
    except Exception as e:
        return ('no', f'eml parse fail: {str(e)[:120]}', '')

def check_text(path):
    try:
        with open(path, 'rb') as f:
            raw = f.read()
        if len(raw) == 0:
            return ('yes', 'empty file (0 bytes)', '')
        try:
            txt = raw.decode('utf-8')
        except UnicodeDecodeError:
            txt = raw.decode('gbk', errors='ignore')
        stripped = txt.strip()
        if not stripped:
            return ('yes', 'whitespace-only file', '')
        return ('yes', f'text {len(raw)} bytes, {len(stripped)} chars', stripped[:200].replace('\n', ' '))
    except Exception as e:
        return ('no', f'read fail: {str(e)[:120]}', '')

def check_special(path, ext):
    # never delete; just describe
    try:
        with open(path, 'rb') as f:
            head = f.read(16)
        size = os.path.getsize(path)
        if ext == '.exe':
            ok = head[:2] == b'MZ'
            return ('unknown', f'exe {size} bytes, MZ header={ok} (special, not deleted)', '')
        if ext in ('.webloc', '.url'):
            try:
                txt = open(path, 'rb').read().decode('utf-8', errors='ignore')[:200]
                return ('unknown', f'{ext} {size} bytes (special, not deleted)', txt.replace('\n', ' '))
            except Exception:
                return ('unknown', f'{ext} {size} bytes (special, not deleted)', '')
        if ext == '.folderstructure' or os.path.basename(path).startswith('.folderstructure'):
            try:
                txt = open(path, 'rb').read().decode('utf-8', errors='ignore')
                return ('unknown', f'.folderstructure {size} bytes (special, not deleted)', txt[:200])
            except Exception:
                return ('unknown', f'.folderstructure {size} bytes (special, not deleted)', '')
        # zap, bolt, osheet, noext
        return ('unknown', f'{ext or "noext"} {size} bytes, head={head[:8]!r} (special, not deleted)', '')
    except Exception as e:
        return ('unknown', f'special read fail: {str(e)[:100]}', '')

IMG_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif', '.gif', '.tiff', '.cr2', '.livp'}
VID_EXTS = {'.mp4', '.mov', '.mts', '.mpg', '.vob'}

def main():
    with open(PENDING_JSON) as f:
        items = json.load(f)
    targets = [it for it in items if '/待复核/' in (it.get('src') or '')]
    print(f'targets: {len(targets)}', flush=True)
    results = []
    for it in targets:
        rid = it['id']
        src = it['src']
        name = os.path.basename(src)
        ext = os.path.splitext(name)[1].lower()
        size = os.path.getsize(src) if os.path.exists(src) else -1
        if not os.path.exists(src):
            verdict, detail, snip = 'no', 'source missing at check time', ''
        elif ext in IMG_EXTS:
            if ext == '.cr2':
                # no rawpy/dcraw: verify TIFF magic only
                with open(src, 'rb') as f:
                    head = f.read(16)
                if head[:2] in (b'II', b'MM') and size > 100000:
                    verdict, detail, snip = 'unknown', f'CR2 {size} bytes, TIFF magic ok but no raw decoder (special, not deleted)', ''
                else:
                    verdict, detail, snip = 'no', f'CR2 bad header/size {size}', ''
            else:
                verdict, detail, snip = check_image(src)
        elif ext in VID_EXTS:
            verdict, detail, snip = check_video(src)
        elif ext == '.pdf':
            verdict, detail, snip = check_pdf(src)
        elif ext == '.xlsx':
            verdict, detail, snip = check_xlsx(src)
        elif ext == '.xls':
            verdict, detail, snip = check_ole(src, 'Workbook')
        elif ext == '.docx':
            verdict, detail, snip = check_zip_xml(src, 'word/document.xml', 'docx')
        elif ext == '.doc':
            verdict, detail, snip = check_ole(src, 'WordDocument')
        elif ext == '.pptx':
            verdict, detail, snip = check_zip_xml(src, 'ppt/presentation.xml', 'pptx')
        elif ext == '.ppt':
            verdict, detail, snip = check_ole(src, 'PowerPoint Document')
        elif ext == '.eml':
            verdict, detail, snip = check_eml(src)
        elif ext in ('.txt', '.md', '.csv', '.json', '.py', '.bat', '.html'):
            verdict, detail, snip = check_text(src)
        elif ext == '.zip':
            verdict, detail, snip = check_zip(src)
        elif ext == '.rar':
            rc, out = sh(f'unrar l {json.dumps(src)} 2>&1 | head -5')
            if rc == 0 and ('ALL OK' in out or 'files' in out.lower()):
                verdict, detail, snip = 'yes', 'unrar list ok', ''
            else:
                # no unrar tool: check Rar! magic
                with open(src, 'rb') as f:
                    head = f.read(8)
                if head[:4] == b'Rar!':
                    verdict, detail, snip = 'unknown', 'rar magic ok, no unrar tool to verify (special, not deleted)', ''
                else:
                    verdict, detail, snip = 'no', 'not a rar file (bad magic)', ''
        else:
            verdict, detail, snip = check_special(src, ext)
        results.append({'id': rid, 'src': src, 'name': name, 'ext': ext,
                        'size': size, 'verdict': verdict, 'detail': detail,
                        'snippet': snip, 'cat': it.get('cat') or '',
                        'conf': it.get('conf'), 'vlm': it.get('vlm') or '',
                        'reason': it.get('reason') or ''})
        print(f"{rid} {verdict} {ext} {name[:60]} :: {detail[:100]}", flush=True)
    with open(OUT, 'w') as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    from collections import Counter
    print('VERDICT:', Counter(r['verdict'] for r in results), flush=True)
    print('BY EXT (no):', Counter(r['ext'] for r in results if r['verdict'] == 'no'), flush=True)
    print('BY EXT (unknown):', Counter(r['ext'] for r in results if r['verdict'] == 'unknown'), flush=True)
    print('saved', OUT, flush=True)
    return 0

if __name__ == '__main__':
    sys.exit(main())
