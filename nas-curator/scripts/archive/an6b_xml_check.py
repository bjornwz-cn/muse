#!/usr/bin/env python3
# an6b: verify content of the two SpreadsheetML .xls files
import re

for rid, p in [(137904, '/vol1/1000/收件箱/待复核/11.1-11.10.xls'),
                (137958, '/vol1/1000/收件箱/待复核/脊柱通用-20-7-12月.xls')]:
    print('=' * 20, rid, p.split('/')[-1])
    with open(p, 'rb') as f:
        chunk = f.read(2 * 1024 * 1024).decode('utf-8', errors='ignore')
    datas = re.findall(r'<Data[^>]*>([^<]{1,80})</Data>', chunk)
    print('  Data cells with text in first 2MB:', len(datas))
    for d in datas[:8]:
        print('   -', d[:80])
    sheets = re.findall(r'Worksheet[^>]*ss:Name="([^"]+)"', chunk)
    print('  worksheets:', sheets[:10])
    # total Row count via string count (cheap)
    with open(p, 'rb') as f:
        data = f.read()
    print('  total <Row occurrences:', data.count(b'<Row'))
    print('  total <Cell occurrences:', data.count(b'<Cell'))
    print('  total <Data occurrences:', data.count(b'<Data'))
