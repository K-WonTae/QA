# app/security.py
"""
파일 접근의 단일 관문. 다른 모듈은 이 함수들만 거쳐 등록된 출처 폴더에 접근한다.

핵심 불변식:
- 접근은 항상 (root, suffixes) 쌍으로 한정된다. root 밖 경로는 절대 통과 못한다.
- 사용자/LLM이 고른 파일명을 직접 경로로 만들지 않는다. 미리 만든
  화이트리스트(allowed dict)의 키로 '정확히 일치'하는 것만 읽는다.
"""
import re
from pathlib import Path

from app.config import (
    MAX_FILE_BYTES,
    MAX_FILES_PER_QUERY,
    MAX_TOTAL_CONTEXT_BYTES,
)
from app.logging_conf import logger

# 본문 추출이 필요한(텍스트가 아닌) 확장자
_EXTRACT_SUFFIXES = {".pdf"}

# 텍스트 파일 디코딩 후보(국내 업무 파일은 CP949/EUC-KR가 흔함).
_TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "cp949", "euc-kr")


def _decode_best(data: bytes) -> str:
    """UTF-8 → CP949 → EUC-KR 순으로 시도하고, 모두 실패하면 대체문자로 읽는다."""
    for enc in _TEXT_ENCODINGS:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def build_allowed_files(root: Path, suffixes: set[str]) -> dict[str, Path]:
    """
    한 출처 폴더를 스캔해 {상대경로 문자열: 절대 Path} 화이트리스트를 만든다.
    - 심볼릭 링크 차단
    - root 밖을 가리키는 항목 차단
    - suffixes 화이트리스트에 든 확장자만 포함
    """
    root = root.resolve()
    suffixes = {s.lower() for s in suffixes}
    allowed: dict[str, Path] = {}
    if not root.is_dir():
        return allowed
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(root):
            continue
        if not resolved.is_file():
            continue
        if resolved.suffix.lower() not in suffixes:
            continue
        rel = resolved.relative_to(root).as_posix()
        allowed[rel] = resolved
    return allowed


def resolve_safe(rel_key: str, allowed: dict[str, Path],
                 root: Path, suffixes: set[str]) -> Path:
    """화이트리스트에 정확히 일치하는 키만 통과시키고 방어적으로 재확인."""
    path = allowed.get(rel_key)
    if path is None:
        raise PermissionError("허용되지 않은 파일 접근")
    root = root.resolve()
    if path.is_symlink() or not path.is_relative_to(root):
        raise PermissionError("허용되지 않은 파일 접근")
    if path.suffix.lower() not in {s.lower() for s in suffixes} or not path.is_file():
        raise PermissionError("허용되지 않은 파일 접근")
    return path


# D-2: 텍스트 파일 디코딩 결과를 (해석경로 → (mtime_ns, size, 본문))로 캐시한다.
# 매 질의마다 같은 폴더의 모든 텍스트를 다시 읽지 않도록 한다.
# 무효화는 mtime/size 변경 기준. PDF는 extract_pdf_file이 별도 mtime 캐시를 갖는다.
_TEXT_CACHE: dict[str, tuple[int, int, str]] = {}


def read_text_safe(rel_key: str, allowed: dict[str, Path],
                   root: Path, suffixes: set[str]) -> str:
    path = resolve_safe(rel_key, allowed, root, suffixes)
    suf = path.suffix.lower()
    # PDF 등: 텍스트가 아니므로 본문을 추출해 반환(자체 크기 상한 적용).
    if suf in _EXTRACT_SUFFIXES:
        from app.pdf_source import extract_pdf_file
        text = extract_pdf_file(path)
        if not text:
            raise ValueError("본문을 추출할 수 없음")
        return text
    # 순수 텍스트 파일 (인코딩 자동 감지: UTF-8/CP949/EUC-KR)
    st = path.stat()
    if st.st_size > MAX_FILE_BYTES:
        raise ValueError("파일 크기 초과")
    # mtime+size 가 같으면 디스크 재읽기/재디코딩을 생략(D-2).
    key = str(path)
    cached = _TEXT_CACHE.get(key)
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    body = _decode_best(path.read_bytes())
    _TEXT_CACHE[key] = (st.st_mtime_ns, st.st_size, body)
    return body


_TOKEN_SPLIT = re.compile(r"[\\/._\-\s()\[\]]+", re.UNICODE)


def _score_file(rel: str, q_lower: str) -> int:
    """파일 경로(폴더명·파일명)와 질문의 토큰 겹침으로 관련도 점수."""
    score = 0
    for tok in _TOKEN_SPLIT.split(rel.lower()):
        if len(tok) >= 2 and tok in q_lower:
            score += 2
    return score


# ── 테이블 우선 → 컬럼 검색 ────────────────────────────────────────────
# 테이블 문서(.md)는 'TableName — 설명' 헤더(##) 아래에 컬럼 표가 붙는 구조다.
# 파일명만으로는 테이블명(SDL_Deunglog_M)·컬럼명·설명이 안 잡히므로,
# 내용을 섹션(=테이블) 단위로 파싱해 ①테이블 매칭을 먼저, ②컬럼 매칭을 그다음으로 본다.
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
_TEXT_RANK_SUFFIXES = {".md", ".txt", ".csv", ".sql"}
_MAX_SCAN_FILES = 400      # 내용 스캔 상한(초과 시 파일명 점수만 사용)
_NAME_W = 3                # 파일명 매칭 가중치
_COL_W = 2                 # 컬럼/본문 매칭 가중치

# 거의 모든 테이블 문서에 흔히 박혀 있어 변별력이 없는 일반어.
# 질문 토큰에서 빼서 '테이블/컬럼' 같은 단어가 헤더와 막 걸리는 노이즈를 줄인다.
_STOPWORDS = {
    "테이블", "컬럼", "필드", "설명", "정보", "내용", "관련", "무엇",
    "어디", "어떻게", "알려줘", "뭐야", "뭔지", "값은", "값이",
    "마스터", "헤더",  # 거의 모든 표 제목에 붙는 접미사 → 변별력 없음
}


def _tokens(text: str) -> set[str]:
    """길이 2 이상 토큰 집합(한글 명사·영문 식별자 단위)."""
    return {t for t in _TOKEN_SPLIT.split(text.lower()) if len(t) >= 2}


def _overlap(q_tokens: set[str], text: str) -> int:
    """질문 토큰과 대상 텍스트 토큰의 교집합 크기."""
    return len(q_tokens & _tokens(text))


def _parse_sections(body: str) -> tuple[str, list[dict]]:
    """본문을 (머리말, [섹션]) 으로 나눈다. 섹션은 '## 헤더'로 시작하는 테이블 블록.
    각 섹션: {'heading': 헤더텍스트, 'text': 헤더+본문 전체}. ## 가 없으면 섹션은 빈 목록."""
    preamble: list[str] = []
    sections: list[dict] = []
    cur: dict | None = None
    for ln in body.split("\n"):
        m = _SECTION_RE.match(ln.strip())
        if m:
            if cur is not None:
                sections.append(cur)
            cur = {"heading": m.group(1).strip(), "lines": [ln]}
        elif cur is not None:
            cur["lines"].append(ln)
        else:
            preamble.append(ln)
    if cur is not None:
        sections.append(cur)
    for s in sections:
        s["text"] = "\n".join(s["lines"]).strip()
    return "\n".join(preamble).strip(), sections


def select_context(allowed: dict[str, Path], root: Path,
                   suffixes: set[str], question: str,
                   budget: int = MAX_TOTAL_CONTEXT_BYTES,
                   max_files: int = MAX_FILES_PER_QUERY,
                   scan_meta: dict | None = None) -> tuple[str, list[str]]:
    """
    질문과 관련도 높은 파일을 화이트리스트 키로만 골라 하나의 컨텍스트로 합친다.

    - 파일 경로 토큰이 질문과 겹치는 문서를 우선(관련도 내림차순).
    - 겹치는 문서가 하나도 없으면 index.md 우선 + 사전순으로 폴백.
    - 개수 상한(min(MAX_FILES_PER_QUERY, max_files))과 총량 상한(budget) 적용.
      budget/max_files 는 호출부가 출처별로 남은 총량·파일 수를 나눠 줄 때 쓴다
      (전역 파일 캡 A-2 연동). max_files<=0 이면 빈 결과.
    - scan_meta(D-1): 텍스트 파일이 너무 많아 '파일명 기반 축소 검색'으로 전환되면
      이 dict에 reduced_scan=True 를 남긴다(호출부가 dev_view 응답 신호로 사용).

    반환: (합쳐진 문서 텍스트, 실제 사용한 파일 키 목록)
    """
    file_cap = min(MAX_FILES_PER_QUERY, max_files)
    if file_cap <= 0 or budget <= 0:
        return "", []
    q_lower = question.lower()
    q_tokens = _tokens(question) - _STOPWORDS

    # 텍스트 파일이 너무 많으면 내용 스캔을 끄고 파일명 점수만 쓴다(비용 방어).
    text_rels = [r for r, p in allowed.items() if p.suffix.lower() in _TEXT_RANK_SUFFIXES]
    scan_content = len(text_rels) <= _MAX_SCAN_FILES
    if not scan_content:
        # D-1: 조용한 품질 저하 방지 — 축소 검색 전환을 경고 로그로 남기고 신호를 올린다.
        logger.warning(
            "reduced_scan: text_files=%d > limit=%d → filename-only ranking (root=%s)",
            len(text_rels), _MAX_SCAN_FILES, root,
        )
        if scan_meta is not None:
            scan_meta["reduced_scan"] = True

    bodies: dict[str, str] = {}     # rel -> 본문(텍스트 파일, 한 번만 읽어 재사용)
    table_hits: list[dict] = []     # 테이블(헤더) 매칭 섹션
    column_hits: list[dict] = []    # 컬럼(본문) 매칭 섹션
    whole_scored: list[tuple[str, int]] = []  # 통째 포함 후보 (rel, score)

    for rel, path in allowed.items():
        suf = path.suffix.lower()
        name_score = _score_file(rel, q_lower)

        # 비텍스트(.pdf 등)·스캔 생략 대상은 파일명 점수만으로 통째 후보.
        if suf not in _TEXT_RANK_SUFFIXES or not scan_content:
            if name_score > 0:
                whole_scored.append((rel, name_score * _NAME_W))
            continue

        try:
            body = read_text_safe(rel, allowed, root, suffixes)
        except (PermissionError, ValueError):
            continue
        bodies[rel] = body

        # 파일명이 질문과 겹치면 그 파일 주제 전체가 부합 → 통째 포함.
        if name_score > 0:
            whole_scored.append((rel, name_score * _NAME_W + _overlap(q_tokens, body)))
            continue

        # 파일명은 안 겹침 → 내용에서 테이블/컬럼 단위로 정밀 검색.
        preamble, sections = _parse_sections(body)
        if not sections:
            ch = _overlap(q_tokens, body)
            if ch > 0:
                whole_scored.append((rel, ch * _COL_W))
            continue
        for sec in sections:
            head_hits = _overlap(q_tokens, sec["heading"])
            body_hits = _overlap(q_tokens, sec["text"])
            if head_hits > 0:                    # ① 테이블명/설명 매칭 우선
                table_hits.append({"rel": rel, "sec": sec, "preamble": preamble,
                                   "head": head_hits, "body": body_hits})
            elif body_hits > 0:                  # ② 컬럼/본문 매칭
                column_hits.append({"rel": rel, "sec": sec, "preamble": preamble,
                                    "head": 0, "body": body_hits})

    # 정렬: 테이블 매칭(헤더↓, 컬럼↓) → 컬럼 매칭(본문↓) → 통째 파일(점수↓)
    table_hits.sort(key=lambda h: (-h["head"], -h["body"], h["rel"]))
    column_hits.sort(key=lambda h: (-h["body"], h["rel"]))
    whole_scored.sort(key=lambda x: (-x[1], x[0]))

    # 아무 매칭도 없으면 기존 폴백(index.md 먼저 + 사전순, 통째).
    if not table_hits and not column_hits and not whole_scored:
        keys = sorted(allowed.keys(),
                      key=lambda k: (0 if k.endswith("index.md") else 1, k))[:file_cap]
        chunks, used, total = [], [], 0
        for rel in keys:
            try:
                body = read_text_safe(rel, allowed, root, suffixes)
            except (PermissionError, ValueError):
                continue
            encoded = len(body.encode("utf-8"))
            if total + encoded > budget:
                break
            chunks.append(f"### 파일: {rel}\n{body}")
            used.append(rel)
            total += encoded
        return "\n\n".join(chunks), used

    # D: 질문이 '폴더명'을 일반 지칭하면 그 폴더의 index.md(폴더 카탈로그/개요)를
    # 섹션 매칭보다 '먼저' 포함한다. 예: "테이블 종류" → 테이블/index.md,
    # "인사 관련" → 인사/index.md. 경로 토큰은 본문 불용어와 무관하게 신호로 쓴다.
    folder_segs: set[str] = set()
    for rel in allowed:
        for seg in rel.split("/")[:-1]:
            if len(seg) >= 2:
                folder_segs.add(seg)
    matched_folders = {seg for seg in folder_segs if seg.lower() in q_lower}
    folder_index_rels: list[str] = []
    if matched_folders:
        for rel in allowed:
            parts = rel.split("/")
            if parts[-1] == "index.md" and len(parts) >= 2 and parts[-2] in matched_folders:
                folder_index_rels.append(rel)
        folder_index_rels.sort()

    # 조립: (D)폴더 카탈로그 → 섹션(테이블→컬럼) → 남는 예산으로 통째 파일.
    chunks: list[str] = []
    used: list[str] = []
    used_set: set[str] = set()
    seen_file_header: set[str] = set()
    seen_section: set[tuple[str, str]] = set()
    total = 0
    n_blocks = 0

    def _add(rel: str, text: str) -> bool:
        nonlocal total, n_blocks
        encoded = len(text.encode("utf-8"))
        if total + encoded > budget:
            return False
        chunks.append(text)
        total += encoded
        n_blocks += 1
        if rel not in used_set:
            used.append(rel)
            used_set.add(rel)
        return True

    # D: 매칭된 폴더의 index.md를 최우선 배치(폴더 전체 목록/개요를 먼저 드러낸다).
    for rel in folder_index_rels:
        if n_blocks >= file_cap:
            break
        if rel in used_set:
            continue
        body = bodies.get(rel)
        if body is None:
            try:
                body = read_text_safe(rel, allowed, root, suffixes)
            except (PermissionError, ValueError):
                continue
        _add(rel, f"### 파일: {rel}\n{body}")

    for hit in table_hits + column_hits:
        if n_blocks >= file_cap:
            break
        rel, sec = hit["rel"], hit["sec"]
        key = (rel, sec["heading"])
        if key in seen_section:
            continue
        seen_section.add(key)
        # 파일 머리말(코드 표기 규칙 등)은 그 파일 첫 블록에서 한 번만 얹는다.
        head = ""
        if rel not in seen_file_header:
            seen_file_header.add(rel)
            head = f"### 파일: {rel}\n"
            if hit["preamble"]:
                head += hit["preamble"] + "\n\n"
        if not _add(rel, head + sec["text"]):
            break

    for rel, _score in whole_scored:
        if n_blocks >= file_cap:
            break
        if rel in used_set:
            continue
        body = bodies.get(rel)
        if body is None:
            try:
                body = read_text_safe(rel, allowed, root, suffixes)
            except (PermissionError, ValueError):
                continue
        if not _add(rel, f"### 파일: {rel}\n{body}"):
            break

    return "\n\n".join(chunks), used
