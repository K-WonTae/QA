# -*- coding: utf-8 -*-
import re, urllib.parse, sys
from pathlib import Path

doc = Path(r"E:\workflow\knowledge\행정\공통코드.md")
base = doc.parent
md = doc.read_text(encoding="utf-8")
links = re.findall(r"\]\((\.{1,2}/.*?\.md)\)", md)
seen, missing, ok = set(), [], []
for l in links:
    if l in seen:
        continue
    seen.add(l)
    target = (base / urllib.parse.unquote(l)).resolve()
    (ok if target.exists() else missing).append(l)

out = Path(r"E:\workflow\data\_verify_result.txt")
lines = [f"unique={len(seen)} ok={len(ok)} missing={len(missing)}"]
lines += ["MISSING: " + m for m in missing]
out.write_text("\n".join(lines), encoding="utf-8")
print("done")
