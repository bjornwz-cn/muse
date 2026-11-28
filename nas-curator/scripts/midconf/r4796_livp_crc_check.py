#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796 深度核查：3 个 CRC 失败的 .livp（容器内，只读）。
对每个文件：逐条目尝试 zipfile 读取；失败的条目再用原始 deflate
解压（忽略 CRC）后尝试 PIL 解码，判断静帧是否真正可恢复；
.mov 条目同样试读（只读前 2MB 解压验证完整性太贵，改为读全量
并校验 CRC——mov 若 CRC 也失败则记 damaged）。
输出每行结论。
用法：docker exec nas-curator python3 /data/r4796_livp_crc_check.py
"""
import io, json, sqlite3, zipfile, zlib
from PIL import Image

DB = '/data/nas-curator.db'
IDS = [51248, 51249, 139388]


def raw_deflate(z, info):
    with open(z.fp.name, 'rb') if hasattr(z, 'fp') else None as fh:
        pass
    return None


def try_entry_raw(src, name):
    """绕过 CRC：按 zip 本地文件头定位压缩数据，zlib 解压。"""
    with open(src, 'rb') as fh:
        data = fh.read()
    # 找本地文件头 PK\x03\x04，逐个解析直到名字匹配
    pos = 0
    while True:
        i = data.find(b'PK\x03\x04', pos)
        if i < 0:
            return None, 'local header not found for %s' % name
        import struct
        (ver, flag, method, mtime, mdate, crc, csize, usize, nlen, elen) = struct.unpack(
            '<HHHHHIIIHH', data[i + 4:i + 30])
        fname = data[i + 30:i + 30 + nlen].decode('utf-8', errors='replace')
        start = i + 30 + nlen + elen
        if fname == name:
            comp = data[start:start + csize]
            if method == 8:
                try:
                    raw = zlib.decompress(comp, -15)
                except Exception as e:
                    return None, 'deflate fail: %s' % e
            elif method == 0:
                raw = comp
            else:
                return None, 'method %d unsupported' % method
            return raw, 'raw-ok len=%d (crc-ignored)' % len(raw)
        pos = start + csize


def main():
    db = sqlite3.connect(DB)
    for rid in IDS:
        src = json.loads(db.execute(
            "SELECT payload FROM review_queue WHERE id=?", (rid,)).fetchone()[0])['src']
        print('=== rq %d %s' % (rid, src))
        z = zipfile.ZipFile(src)
        for info in z.infolist():
            print(' entry %s method=%d csize=%d usize=%d' % (
                info.filename, info.compress_type, info.compress_size, info.file_size))
            try:
                raw = z.read(info.filename)
                print('  zipfile read OK len=%d' % len(raw))
            except Exception as e:
                print('  zipfile read FAIL: %s' % e)
                raw2, note = try_entry_raw(src, info.filename)
                print('  raw retry:', note)
                if raw2 is not None and not info.filename.lower().endswith('.mov'):
                    try:
                        import pillow_heif  # noqa
                        pillow_heif.register_heif_opener()
                        img = Image.open(io.BytesIO(raw2))
                        img.verify()
                        print('  PIL verify OK after raw recover: format=%s size=%s' % (img.format, img.size))
                    except Exception as e2:
                        print('  PIL verify FAIL after raw recover: %s' % e2)


if __name__ == '__main__':
    main()
