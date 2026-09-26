"""Reproducible full-workbook row audit; run: python analysis/audit_reports.py."""
from pathlib import Path
from collections import Counter, defaultdict
import json, re, sys
import openpyxl
ROOT=Path(__file__).resolve().parents[1]

def model_date(path):
    m=re.fullmatch(r'paper_test_(?:(M\d+[A-Z]?)_)?(\d{4}-\d{2}-\d{2})\.xlsx',path.name)
    return ((m[1] or 'M1'),m[2]) if m else None

def parse():
    rows=[]; manifests=[]
    for path in sorted(ROOT.glob('paper_test*.xlsx')):
        key=model_date(path)
        if not key:continue
        wb=openpyxl.load_workbook(path,read_only=True,data_only=True)
        notes=Counter(); trades=[]; weird=[]; section=''
        for sh in wb:
            head=None
            for i,r in enumerate(sh.values,1):
                if r and r[0]=='Date' and 'Symbol' in r and 'Side' in r:
                    head={str(v):j for j,v in enumerate(r) if v is not None};section='trades';continue
                if not head:continue
                get=lambda col: r[head[col]] if col in head and head[col]<len(r) else None
                if get('Side') in ('BUY','SELL') and get('Symbol') and isinstance(get('P&L ₹'),(int,float)):
                    cost=get('Costs+Slip ₹'); net=get('Net P&L ₹')
                    if net is None and isinstance(cost,(int,float)):net=get('P&L ₹')-cost
                    a={'model':key[0],'day':key[1],'file':path.name,'sheet':sh.title,'row':i,
                       'sym':str(get('Symbol')),'side':get('Side'),'signal':str(get('Signal') or ''),
                       'setup':str(get('Setup') or ''),'rank':get('SpurtRank'),
                       'time':str(get('Entry time') or ''),'exit':str(get('Exit') or ''),
                       'pnl':get('P&L ₹'),'cost':cost,'net':net,'sl':get('SL ₹'),'entry':get('Entry ₹')}
                    trades.append(a);rows.append(a)
                    if section!='trades':weird.append(f'trade outside primary block row {i}')
                elif r[0] is not None:
                    text=str(r[0]);
                    if any(w in text.upper() for w in ('SKIPPED','GHOST','SHADOW','TOTAL','SUMMARY','GATE','CANDIDATE')):
                        notes[text[:95]]+=1
                        if 'SKIPPED' in text.upper() or 'GHOST' in text.upper():section='disclosures'
            # A non-primary sheet remains accounted for via sheet list.
        manifests.append({'file':path.name,'model':key[0],'day':key[1], 'sheets':wb.sheetnames,
                          'trades':len(trades),'net_available':sum(x['net'] is not None for x in trades),
                          'open_marks':sum(x['exit']=='OPEN' for x in trades),
                          'notes':dict(notes),'warnings':weird})
        wb.close()
    return rows, manifests

if __name__=='__main__':
    rows,manifest=parse()
    out=ROOT/'analysis'/'output';out.mkdir(exist_ok=True)
    (out/'report_manifest.json').write_text(json.dumps(manifest,indent=2,default=str))
    (out/'trade_rows.json').write_text(json.dumps(rows,default=str))
    print('reports',len(manifest),'trade rows',len(rows),'missing net',sum(x['net'] is None for x in rows))
    for model in sorted({x['model'] for x in manifest}):
        a=[x for x in rows if x['model']==model and isinstance(x['net'],(int,float))]
        print(model,'reports',sum(m['model']==model for m in manifest),'net trades',len(a),'wins',sum(x['net']>0 for x in a), 'net',round(sum(x['net'] for x in a)))
