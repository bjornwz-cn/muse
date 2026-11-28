#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r4796 深度核查：51248 mov 区域是否全零/损坏（容器内，只读）。"""
import os

SRC = '/vol1/1000/收件箱/媒体库/视频/2025/20250209_0007.livp'


def main():
    data = open(SRC, 'rb').read()
    print('file size:', len(data))
    region = data[1946975:1946975 + 4663025]
    print('mov region len:', len(region))
    print('nonzero bytes in mov region:', sum(1 for b in region if b != 0))
    print('first 64 of region:', region[:64].hex())
    print('sample +1MB:', region[1000000:1000064].hex())
    print('sample +2MB:', region[2000000:2000064].hex())
    print('sample +4MB:', region[4000000:4000064].hex())
    print('last 64:', region[-64:].hex())
    print('ftyp in file:', data.find(b'ftyp'), 'moov:', data.find(b'moov'),
          'mdat:', data.find(b'mdat'))
    # 全盘找同名静帧是否另有副本
    for pat in ('IMG_1489', 'IMG_1490'):
        hits = []
        for root, _dirs, files in os.walk('/vol1/1000'):
            for fn in files:
                if pat in fn:
                    hits.append(os.path.join(root, fn))
        print(pat, 'copies on disk:', hits)


if __name__ == '__main__':
    main()
