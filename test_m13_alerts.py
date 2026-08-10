#!/usr/bin/env python3
import json
import os
import tempfile
from pathlib import Path

import m13_alerts as A


def ok(name, cond):
    assert cond, name
    print("PASS", name)


def main():
    keys=[f"{p}_{k}_{s}" for p in ("M13","M11") for k in ("BOT_TOKEN","CHAT_ID") for s in ("A","B")]
    saved={k:os.environ.get(k) for k in keys};old=(A.tg.send_message,A.tg._post,A.tg.send_document);calls=[]
    try:
        for k in keys:os.environ.pop(k,None)
        A.tg.send_message=lambda text,silent=False:calls.append(("main",text,silent))
        A.tg._post=lambda url,**kw:calls.append(("extra",url,kw))
        A.tg.send_document=lambda path,caption="":calls.append(("main-doc",path,caption))
        ok("main-only fallback",A.send_message("x")==1 and len(calls)==1);calls.clear()
        for s in ("A","B"):
            os.environ[f"M11_BOT_TOKEN_{s}"]=f"fallback-token-{s}"
            os.environ[f"M11_CHAT_ID_{s}"]=f"fallback-chat-{s}"
        ok("M11 fallback creates 3 targets",A.send_message("x")==3 and len(calls)==3);calls.clear()
        os.environ['M13_BOT_TOKEN_A']='m13-token-A';os.environ['M13_CHAT_ID_A']='m13-chat-A'
        ok("M13-specific A overrides fallback A",A.send_message("x")==3 and any('m13-token-A' in str(c) for c in calls));calls.clear()
        with tempfile.NamedTemporaryFile(suffix='.xlsx') as f:
            ok("document reaches 3 targets",A.send_document(f.name,'r')==3 and len(calls)==3)
        with tempfile.TemporaryDirectory() as td:
            fp=Path(td)/'state.json'
            def save(st):fp.write_text(json.dumps(st))
            st={'alerts':[]}
            ok("first reservation persists",A.reserve_once(st,'T:ENTRY',save) and 'T:ENTRY' in json.loads(fp.read_text())['alerts'])
            ok("duplicate reservation is rejected",not A.reserve_once(st,'T:ENTRY',save) and st['alerts'].count('T:ENTRY')==1)
            owned=A.reserve_batch(st,['EOD:S','EOD:D','T:ENTRY'],save)
            ok("batch owns only new keys",owned=={'EOD:S','EOD:D'})
            ok("replayed batch owns nothing",not A.reserve_batch(st,['EOD:S','EOD:D'],save))
    finally:
        A.tg.send_message,A.tg._post,A.tg.send_document=old
        for k,v in saved.items():
            if v is None:os.environ.pop(k,None)
            else:os.environ[k]=v
    print('ALL M13 ALERT TESTS PASSED')

if __name__=='__main__':main()
