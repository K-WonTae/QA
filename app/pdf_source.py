# app/pdf_source.py
"""
'규정' 출처(kind=pdf_index) 처리.

knowledge/학사/규정/*.md 의 표에는 규정 제목과 PDF 링크(www.kosin.ac.kr)가 들어있다.
질의가 들어오면:
  1) 목록 .md 들을 파싱해 (제목, URL) 항목을 모으고
  2) 질문 키워드와 제목을 매칭해 상위 N개를 고른 뒤
  3) 해당 PDF를 '실시간' 다운로드 → 텍스트 추출(캐시) → 컨텍스트로 합친다.

보안:
- 다운로드는 PDF_FETCH_ALLOWLIST 호스트(https)만 허용한다(SSRF 방어).
- 크기/타임아웃/개수/문자수 상한을 모두 적용한다.
- 추출 본문은 호출부(prompt_builder)에서 데이터로 격리된다(프롬프트 인젝션 방어).
"""
import hashlib
import io
import json
import re
import ssl
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from app.config import (
    PDF_FETCH_ALLOWLIST,
    PDF_FETCH_TIMEOUT,
    MAX_PDF_BYTES,
    MAX_PDF_FETCH,
    MAX_PDF_TEXT_CHARS,
    MAX_TOTAL_CONTEXT_BYTES,
    PDF_CACHE_DIR,
    REG_SELECT_MODEL,
)

# 마크다운 링크: [텍스트](URL)
_LINK_RE = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")
_TOKEN_SPLIT = re.compile(r"[\\/._\-\s()\[\]·,]+", re.UNICODE)


def parse_index(root: Path) -> list[dict]:
    """
    규정 목록 폴더의 .md 들을 파싱해 [{title, url}] 목록을 만든다.
    표 행에서 URL을 찾고, 같은 행에서 URL 셀 바로 앞 셀을 제목으로 본다.
    중복 URL은 한 번만 담는다.
    """
    items: list[dict] = []
    seen: set[str] = set()
    if not root.is_dir():
        return items
    for md in sorted(root.rglob("*.md")):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = _LINK_RE.search(line)
            if not m:
                continue
            url = m.group(1)
            host = (urlparse(url).hostname or "").lower()
            if host not in PDF_FETCH_ALLOWLIST:
                continue
            if url in seen:
                continue
            title = _extract_title(line, m.start())
            if not title:
                continue
            seen.add(url)
            items.append({"title": title, "url": url, "file": md.name})
    return items


def _extract_title(line: str, link_pos: int) -> str:
    """표 행에서 링크 셀 직전 셀을 제목으로 추출. 표가 아니면 링크 앞 텍스트."""
    if "|" in line:
        cells = [c.strip() for c in line.split("|")]
        cells = [c for c in cells if c != ""]
        # 링크를 포함한 셀의 인덱스를 찾아 그 앞 셀을 제목으로
        for i, c in enumerate(cells):
            if "](" in c or c.startswith("[") :
                if i > 0:
                    cand = cells[i - 1]
                    # 숫자만 있는 셀(no/idx)은 건너뛰고 그 앞을 본다
                    j = i - 1
                    while j >= 0 and cells[j].isdigit():
                        j -= 1
                    if j >= 0:
                        return cells[j]
                break
    # 폴백: 링크 앞쪽 텍스트 일부
    head = line[:link_pos].strip(" |").strip()
    return head[-60:] if head else ""


def list_titles(root: Path, limit: int = 80) -> list[str]:
    """규정 제목 목록(되묻기/안내용)."""
    return [it["title"] for it in parse_index(root)][:limit]


def _score_title(title: str, q_lower: str) -> int:
    score = 0
    for tok in _TOKEN_SPLIT.split(title.lower()):
        if len(tok) >= 2 and tok in q_lower:
            score += 3
    return score


def select_regulations(items: list[dict], question: str,
                       max_n: int = MAX_PDF_FETCH) -> list[dict]:
    """질문 키워드와 제목을 매칭해 상위 max_n개 규정을 고른다(폴백용)."""
    q = question.lower()
    scored = [(it, _score_title(it["title"], q)) for it in items]
    relevant = [it for it, s in scored if s > 0]
    relevant.sort(key=lambda it: -_score_title(it["title"], q))
    return relevant[:max_n]


def _needs_llm_disambiguation(chosen: list[dict]) -> bool:
    """키워드 매칭 결과가 '모호'한지 판정한다(A-1).
    - 정확히 1개로 좁혀지면 명확 → LLM 선택 호출을 생략(False).
    - 0개(매칭 실패) 또는 2개 이상(어느 규정인지 불명확)이면 LLM로 의도 추론(True).
    """
    return len(chosen) != 1


def _parse_indices(raw: str, n: int) -> list[int]:
    """LLM 선택 결과 JSON에서 유효 인덱스만 추출(범위 검증)."""
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return []
    out: list[int] = []
    for v in (data.get("indices", []) if isinstance(data, dict) else []):
        if isinstance(v, bool):
            continue
        if isinstance(v, int) and 0 <= v < n and v not in out:
            out.append(v)
    return out


def select_regulations_llm(items: list[dict], question: str,
                           max_n: int = MAX_PDF_FETCH) -> list[dict]:
    """
    질문 의도를 보고 '열어야 할 규정'을 LLM이 제목 목록에서 고르게 한다.
    (예: '휴학 몇 번' → 학칙). LLM 출력은 인덱스 화이트리스트로만 신뢰한다.
    실패 시 빈 목록 → 호출부가 키워드 매칭으로 폴백한다.
    """
    # 지연 임포트(순환 방지) — claude_runner는 config만 의존한다.
    from app.claude_runner import run_claude
    from app.prompt_builder import build_reg_select_prompt

    numbered = "\n".join(f"{i}. {it['title']}" for i, it in enumerate(items))
    prompt = build_reg_select_prompt(question, numbered, max_n)
    try:
        # 규정 선택은 가벼운 작업이므로 경량 모델(haiku)로 비용/지연을 줄인다 (A-1).
        raw = run_claude(prompt, model=REG_SELECT_MODEL)
    except Exception:
        return []
    idxs = _parse_indices(raw, len(items))
    return [items[i] for i in idxs[:max_n]]


def _cache_path(url: str) -> Path:
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return PDF_CACHE_DIR / f"{h}.txt"


def _ssl_context() -> ssl.SSLContext:
    # 인증서 정상 검증(테스트로 유효 확인됨). 비활성화하지 않는다.
    return ssl.create_default_context()


def fetch_pdf_text(url: str) -> str:
    """
    PDF를 다운로드해 텍스트로 추출한다(캐시 사용).
    실패 시 빈 문자열을 반환(호출부에서 건너뜀).
    """
    host = (urlparse(url).hostname or "").lower()
    if not url.lower().startswith("https://") or host not in PDF_FETCH_ALLOWLIST:
        return ""

    cache = _cache_path(url)
    if cache.is_file():
        try:
            return cache.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=PDF_FETCH_TIMEOUT,
                                    context=_ssl_context()) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "application/pdf" not in ctype:
                return ""
            data = resp.read(MAX_PDF_BYTES + 1)
        if len(data) > MAX_PDF_BYTES:
            return ""
    except Exception:
        return ""

    text = _extract_pdf_text(data)
    if not text:
        return ""
    if len(text) > MAX_PDF_TEXT_CHARS:
        text = text[:MAX_PDF_TEXT_CHARS] + "\n…(이하 생략)"

    try:
        PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
    except OSError:
        pass
    return text


def extract_pdf_file(path: Path) -> str:
    """
    로컬 PDF 파일의 본문을 추출한다(파일 경로+수정시각 기준 캐시).
    실패/초과 시 빈 문자열. 등록된 폴더 출처에서 .pdf 를 읽을 때 사용.
    """
    try:
        st = path.stat()
    except OSError:
        return ""
    if st.st_size > MAX_PDF_BYTES:
        return ""

    key = f"{path}|{st.st_mtime_ns}|{st.st_size}"
    cache = PDF_CACHE_DIR / (hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".txt")
    if cache.is_file():
        try:
            return cache.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass

    try:
        data = path.read_bytes()
    except OSError:
        return ""
    text = _extract_pdf_text(data)
    if not text:
        return ""
    if len(text) > MAX_PDF_TEXT_CHARS:
        text = text[:MAX_PDF_TEXT_CHARS] + "\n…(이하 생략)"
    try:
        PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
    except OSError:
        pass
    return text


def _extract_pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages:
            t = page.extract_text() or ""
            if t:
                parts.append(t)
        return "\n".join(parts).strip()
    except Exception:
        return ""


def build_pdf_context(root: Path, question: str,
                      budget: int = MAX_TOTAL_CONTEXT_BYTES,
                      max_files: int = MAX_PDF_FETCH) -> tuple[str, list[str]]:
    """
    규정 출처용 컨텍스트 구성.
    budget: 이 출처가 쓸 수 있는 문서 총량 상한(연계 질의에서 출처별로 나눠 쓴다).
    max_files: 이 출처가 고를 수 있는 최대 규정 수(전역 파일 캡 A-2 연동).
    반환: (합쳐진 문서 텍스트, 실제 사용한 규정 표시 목록)
    표시 목록은 '제목' 문자열(파일 경로가 아님).
    """
    file_cap = min(MAX_PDF_FETCH, max_files)
    if file_cap <= 0 or budget <= 0:
        return "", []
    items = parse_index(root)
    if not items:
        return "", []

    # 1차: 질문 키워드/제목 매칭 → 2차: 좁히지 못해 모호하면 경량 모델 LLM 선택 (A-1)
    chosen = select_regulations(items, question, max_n=file_cap)
    if _needs_llm_disambiguation(chosen):
        llm = select_regulations_llm(items, question, max_n=file_cap)
        if llm:
            chosen = llm
    chosen = chosen[:file_cap]

    chunks: list[str] = []
    used: list[str] = []
    total = 0
    for it in chosen:
        body = fetch_pdf_text(it["url"])
        if not body:
            continue
        block = f"### 규정: {it['title']}\n(출처: {it['url']})\n{body}"
        encoded = len(block.encode("utf-8"))
        if total + encoded > budget:
            break
        chunks.append(block)
        used.append(it["title"])
        total += encoded

    return "\n\n".join(chunks), used
