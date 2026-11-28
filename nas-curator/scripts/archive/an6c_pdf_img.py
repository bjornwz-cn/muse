#!/usr/bin/env python3
# an6c: for PDFs with no extractable text on page 1, check whether pages
# contain images (scanned content) or are truly blank.
import json
from pypdf import PdfReader

rows = [json.loads(l) for l in open('/data/an6_check.jsonl')]
targets = [r for r in rows if r['ext'] == '.pdf' and r['verdict'] == 'yes'
           and not (r.get('snippet') or '').strip()]
print('PDFs with no text on p1:', len(targets))
for r in targets:
    try:
        rd = PdfReader(r['src'])
        n = len(rd.pages)
        imgs = 0
        txt_pages = 0
        for pg in rd.pages:
            try:
                if (pg.extract_text() or '').strip():
                    txt_pages += 1
            except Exception:
                pass
            try:
                res = pg.get('/Resources') or {}
                xo = res.get('/XObject') if hasattr(res, 'get') else None
                if xo:
                    obj = xo.get_object()
                    for k in obj:
                        try:
                            sub = obj[k].get_object()
                            if sub.get('/Subtype') == '/Image':
                                imgs += 1
                        except Exception:
                            pass
            except Exception:
                pass
        print(r['id'], r['name'][:55], '| pages:', n, '| pages w/ text:', txt_pages, '| images:', imgs)
    except Exception as e:
        print(r['id'], r['name'][:55], '| ERROR:', str(e)[:100])
