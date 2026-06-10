# -*- coding: utf-8 -*-
import os, unicodedata
from pathlib import Path

res = []
haeng = Path(r"E:\workflow\knowledge\행정")
res.append("행정 listdir:")
for f in os.listdir(haeng):
    res.append(f"  {f!r} NFC={unicodedata.is_normalized('NFC', f)} NFD={unicodedata.is_normalized('NFD', f)}")

insa = None
for f in os.listdir(haeng):
    if unicodedata.normalize("NFC", f) == "인사":
        insa = haeng / f
        break
res.append(f"insa resolved on disk: {insa!r}")
if insa:
    files = os.listdir(insa)[:3]
    for f in files:
        res.append(f"  file {f!r} NFC={unicodedata.is_normalized('NFC', f)}")

# 학사 공통코드 폴더도 비교
hs = Path(r"E:\workflow\knowledge\학사\공통코드\장학")
if hs.exists():
    for f in os.listdir(hs)[:2]:
        res.append(f"  학사장학 {f!r} NFC={unicodedata.is_normalized('NFC', f)}")

Path(r"E:\workflow\data\_diag_out.txt").write_text("\n".join(res), encoding="utf-8")
print("done")
