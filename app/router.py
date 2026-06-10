# app/router.py
"""
요청 진입점 로직: 입력 검증, 구분자 라우팅, 오케스트레이션, 에러 매핑.
신뢰할 수 없는 입력(질문/문서/LLM 출력)을 경로·명령으로 쓰지 않는다.

질의 흐름:
  클라이언트가 source_id를 보내면 해당 폴더에 질의한다.
  source_id가 없으면 질문 맨 앞의 '구분자'(@지식, @학사 등)를 호환 처리한다.
  둘 다 없으면 최근 세션 출처를 쓰거나, 등록된 출처 목록을 반환한다.
"""
import re
import time
from pathlib import Path

from app.config import (
    MAX_QUESTION_LEN, MAX_TOTAL_CONTEXT_BYTES,
    MAX_FILES_TOTAL, MIN_SOURCE_CONTEXT_BYTES,
)
from app.security import build_allowed_files, select_context
from app.sources import parse_delimiter, parse_suffix_set
from app.pdf_source import build_pdf_context, list_titles
from app.prompt_builder import build_answer_prompt
from app.claude_runner import run_claude
from app.database import (
    save_message, get_recent_messages, ensure_session,
    list_sources, get_source_by_delimiter,
    get_source_by_id, get_linked_sources,
    get_session_source, set_session_source,
)
from app.logging_conf import log_query


# 12장: 내부 예외를 사용자에게 그대로 노출하지 않는다. 코드→메시지 매핑만 반환.
ERROR_MESSAGES = {
    "QUESTION_TOO_LONG": "질문이 너무 깁니다. 줄여서 다시 입력해주세요.",
    "EMPTY_QUESTION": "질문을 입력해주세요.",
    "INVALID_INPUT": "잘못된 입력입니다.",
    "CLAUDE_TIMEOUT": "응답 생성이 지연되어 중단되었습니다. 다시 시도해주세요.",
    "CLAUDE_FAILED": "답변 생성 중 오류가 발생했습니다.",
    "PermissionError": "허용되지 않은 요청입니다.",
    "AUTH_REQUIRED": "로그인이 필요합니다.",
    "FORBIDDEN": "권한이 없습니다. 관리자만 접근할 수 있습니다.",
    "INTERNAL": "처리 중 오류가 발생했습니다.",
}


def error_message(code: str) -> str:
    return ERROR_MESSAGES.get(code, ERROR_MESSAGES["INTERNAL"])


def validate_question(q: str) -> str:
    if not isinstance(q, str):
        raise ValueError("INVALID_INPUT")
    q = q.strip()
    if not q:
        raise ValueError("EMPTY_QUESTION")
    if len(q) > MAX_QUESTION_LEN:
        raise ValueError("QUESTION_TOO_LONG")
    return q


def _build_session_summary(session_id: str) -> str:
    """최근 메시지를 짧게 요약(절단)해 컨텍스트로 전달. 본문은 로그에 남기지 않는다."""
    rows = get_recent_messages(session_id, limit=4)
    parts = []
    for role, content in rows:
        snippet = content.strip().replace("\n", " ")
        if len(snippet) > 200:
            snippet = snippet[:200] + "…"
        parts.append(f"{role}: {snippet}")
    return " / ".join(parts)


def _source_choices() -> list[dict]:
    """되묻기 화면에 보여줄 (구분자, 라벨) 목록."""
    return [
        {
            "id": s["id"],
            "delimiter": s["delimiter"],
            "label": s["label"],
            "division": s.get("division", "학사"),
        }
        for s in list_sources(enabled_only=True)
    ]


def _gather_source_docs(src: dict, question: str, budget: int,
                        max_files: int,
                        scan_meta: dict | None = None) -> tuple[str, list[str]]:
    """한 출처에서 질문 관련 문서를 모은다(local/pdf_index 공통 관문).
    반환: (문서 텍스트, 사용한 파일/규정 표시 목록).
    budget(바이트)·max_files(개수) 안에서만 채운다(전역 예산/파일 캡 연동).
    scan_meta: 축소 검색 전환 신호를 호출부로 올리는 통로(D-1)."""
    root = Path(src["root_path"])
    if src.get("kind") == "pdf_index":
        return build_pdf_context(root, question, budget, max_files=max_files)
    suffixes = parse_suffix_set(src["suffixes"])
    allowed = build_allowed_files(root, suffixes)
    return select_context(allowed, root, suffixes, question, budget,
                          max_files=max_files, scan_meta=scan_meta)


def _sources_from_used(used_files: list[str]) -> list[dict]:
    """표시 문자열('@출처 · 경로')을 구조화 출처 목록[{path,title,source}]으로 변환(S-0).
    path 는 모델이 인용할 닫힌 집합 키이자 다운로드용 상대경로, source 는 출처 구분자."""
    out: list[dict] = []
    for u in used_files:
        parts = u.split(" · ", 1)
        if len(parts) == 2:
            out.append({"path": parts[1], "title": u, "source": parts[0]})
        else:
            out.append({"path": u, "title": u, "source": ""})
    return out


def _build_catalog(src: dict, max_chars: int = 2000) -> str:
    """이 출처에 '어떤 문서/테이블이 있는지' 보여줄 경량 목차를 만든다(B).
    - pdf_index(@규정): 규정 제목 목록.
    - local: 상위 2단계 폴더 구조만(파일 본문은 넣지 않아 토큰을 작게 유지).
    상세 목록은 D(폴더 index.md 우선 포함)와 결정적 카탈로그(A)가 담당한다.
    """
    root = Path(src["root_path"])
    if src.get("kind") == "pdf_index":
        titles = list_titles(root)
        body = "\n".join(f"- {t}" for t in titles)
        return body[:max_chars]
    lines: list[str] = []
    try:
        for top in sorted((p for p in root.iterdir() if p.is_dir()),
                          key=lambda p: p.name):
            subs = sorted(c.name for c in top.iterdir() if c.is_dir())
            lines.append(f"- {top.name}: " + ", ".join(subs) if subs else f"- {top.name}")
    except OSError:
        return ""
    return "\n".join(lines)[:max_chars]


# A: '목록/종류' 의도 감지. 의도어 + 대상어가 함께 있을 때만 인정해 오탐을 줄인다.
# 예) "테이블 종류 알려줘"(O), "장학세부분류 코드값 알려줘"(X: 의도어 없음).
_INV_INTENT_RE = re.compile(
    r"(목록|리스트|종류|일람|카탈로그|어떤|무슨|뭐가?\s*있|전체|전부|모든|list|all)", re.I)
_INV_TARGET_RE = re.compile(
    r"(테이블|문서|코드|규정|항목|파일|자료|목차)", re.I)


def _is_inventory_query(question: str) -> bool:
    """'무엇이 있는지'를 묻는 목록 질의인가(내용 질의와 구분)."""
    return bool(_INV_INTENT_RE.search(question) and _INV_TARGET_RE.search(question))


# 파일명 끝의 코드 표기 "(SH02)" 등을 떼어 한글 본명만 남긴다.
_DOC_CODE_SUFFIX_RE = re.compile(r"\([^)]*\)\s*$")


def _mentions_specific_item(src: dict, question: str) -> bool:
    """질문이 '특정 문서/테이블 이름'을 직접 가리키는가.
    가리키면 그건 내용 질의이므로 목록 즉답(A)을 발동하지 않는다.
    예) '장학세부분류 코드값…' → 장학세부분류(SH02) 문서를 지칭 → True."""
    if src.get("kind") == "pdf_index":
        return False  # 규정은 목록 질의여도 제목 목록 즉답이 적절하므로 가드 불필요
    try:
        suffixes = parse_suffix_set(src["suffixes"])
        allowed = build_allowed_files(Path(src["root_path"]), suffixes)
    except Exception:
        return False
    for rel in allowed:
        stem = rel.rsplit("/", 1)[-1]
        if stem == "index.md":
            continue
        if stem.endswith(".md"):
            stem = stem[:-3]
        base = _DOC_CODE_SUFFIX_RE.sub("", stem).strip()
        if len(base) >= 2 and base in question:
            return True
    return False


def _build_inventory_answer(src: dict) -> tuple[str, list[dict]]:
    """LLM 없이 결정적으로 만드는 목록 응답(A). 출처에 등록된 문서/규정을 폴더별로 나열.
    상세 내용이 필요하면 이름을 넣어 다시 물으라는 안내를 덧붙인다.
    반환: (답변 텍스트, 집계 근거 출처 목록[{path,title}]). 근거 없으면 ("", [])."""
    root = Path(src["root_path"])
    label = src["label"]
    delim = src["delimiter"]
    if src.get("kind") == "pdf_index":
        titles = list_titles(root)
        if not titles:
            return "", []
        bullets = "\n".join(f"- {t}" for t in titles)
        answer = (
            f"## {label} 목록 ({len(titles)}개)\n{bullets}\n\n"
            f"특정 규정의 상세 내용이 필요하면 그 이름을 넣어 다시 물어봐 주세요. "
            f"예) `{delim} {titles[0]} 내용`"
        )
        return answer, []

    suffixes = parse_suffix_set(src["suffixes"])
    allowed = build_allowed_files(root, suffixes)
    groups: dict[str, list[str]] = {}
    index_rels: list[str] = []
    for rel in sorted(allowed):
        parts = rel.rsplit("/", 1)
        folder = parts[0] if len(parts) == 2 else "(루트)"
        name = parts[-1]
        if name == "index.md":
            index_rels.append(rel)        # 목록 집계의 근거 출처(F-1.5)
            continue
        stem = name[:-3] if name.endswith(".md") else name
        groups.setdefault(folder, []).append(stem)
    if not groups:
        return "", []

    total = sum(len(v) for v in groups.values())
    blocks = [f"## {label} 문서/테이블 목록", f"총 {total}개 문서가 등록되어 있습니다.\n"]
    for folder in sorted(groups):
        items = groups[folder]
        blocks.append(f"### {folder} ({len(items)}개)\n"
                      + "\n".join(f"- {n}" for n in items))
    blocks.append(
        f"\n특정 문서의 상세 내용이 필요하면 그 이름을 질문에 넣어 다시 물어봐 주세요. "
        f"예) `{delim} <문서명> 내용`"
    )
    sources = [{"path": r, "title": f"{delim} {r}", "source": delim} for r in index_rels]
    return "\n\n".join(blocks), sources


def _gather_with_links(primary: dict, question: str) -> tuple[str, list[str], list[str]]:
    """주 출처 + 연계 출처들의 문서를 출처별 머리표로 구분해 하나로 병합한다.

    - 주 출처를 먼저, 그 다음 등록된 연계 출처 순으로 채운다(주 출처 우선).
    - 전체 총량 상한(MAX_TOTAL_CONTEXT_BYTES)과 전역 파일 수 상한(MAX_FILES_TOTAL)을
      출처들이 앞에서부터 나눠 쓰되, 뒤 출처에 최소 예산(MIN_SOURCE_CONTEXT_BYTES)을
      남겨 굶지 않게 한다(A-2/A-4). 배정 예산·파일 수가 0이면 스캔 자체를 건너뛴다.
    - 머리표 바이트도 남은 예산에서 차감해 실제 전송량이 상한을 넘지 않게 한다(B-4).
    - 표시 파일명에는 출처 구분자를 접두로 붙여 어디서 왔는지 드러낸다.

    반환: (병합 문서, 사용 표시 목록, 실제 기여한 연계 출처 라벨 목록, 스캔 메타)
    """
    sources = [primary] + get_linked_sources(primary["id"])
    n = len(sources)
    blocks: list[str] = []
    used: list[str] = []
    linked_labels: list[str] = []
    scan_meta: dict = {}        # D-1: 축소 검색 전환 신호 수집
    remaining = MAX_TOTAL_CONTEXT_BYTES
    files_left = MAX_FILES_TOTAL

    for i, s in enumerate(sources):
        if remaining <= 0 or files_left <= 0:
            break
        # 뒤에 남은 출처들에 최소 예산을 남겨두고 이 출처가 쓸 몫을 정한다(A-4).
        sources_left = n - i
        reserve_for_rest = MIN_SOURCE_CONTEXT_BYTES * (sources_left - 1)
        alloc = remaining - reserve_for_rest
        header = f"[출처: {s['delimiter']} {s['label']}]"
        header_bytes = len((header + "\n").encode("utf-8"))  # 머리표도 예산에 반영(B-4)
        doc_budget = alloc - header_bytes
        # 배정 예산·파일 수가 0 이하면 디스크 스캔 없이 건너뛴다(A-4 낭비 제거).
        if doc_budget <= 0:
            continue
        docs, files = _gather_source_docs(s, question, doc_budget, files_left,
                                          scan_meta=scan_meta)
        if not files:
            continue
        block = f"{header}\n{docs}"
        blocks.append(block)
        used.extend(f"{s['delimiter']} · {f}" for f in files)
        remaining -= len(block.encode("utf-8"))  # 머리표+문서 전체를 차감(B-4)
        files_left -= len(files)
        if i > 0:
            linked_labels.append(s["label"])

    return "\n\n".join(blocks), used, linked_labels, scan_meta


def prepare_ask(question: str, session_id: str, source_id: int | None = None,
                dev_view: bool = False) -> dict:
    """
    CLI 호출 '전' 단계(빠름·블로킹 없음): 입력 검증 → 구분자 라우팅 →
    문서 선택 → 프롬프트 구성.

    반환(둘 중 하나):
      - {"needs_source": True, "sources": [...]}  구분자 없음 → 되묻기
      - {"needs_source": False, "question","category","files","prompt"}
    예외: ValueError(코드)
    """
    q = validate_question(question)

    enabled = list_sources(enabled_only=True)
    known = {s["delimiter"] for s in enabled}
    src = None
    stripped = q
    delim = None

    if isinstance(source_id, int):
        src = get_source_by_id(source_id)
        if src is None or not src["enabled"]:
            return {"needs_source": True, "sources": _source_choices()}
        delim = src["delimiter"]
    else:
        delim, stripped = parse_delimiter(q, known)

        # 구분자 없음 → 최근 출처를 쓰거나 어느 폴더에 물을지 되묻는다.
        if delim is None:
            last_delim = get_session_source(session_id)
            if last_delim in known:
                delim = last_delim
                stripped = q
            else:
                return {"needs_source": True, "sources": _source_choices()}

    # 구분자만 있고 실제 질문이 비었으면 다시 입력 요청.
    if not stripped:
        raise ValueError("EMPTY_QUESTION")

    # 구분자 경로(소스 직접지정이 아닌 경우)에서는 delim으로 src를 해석한다.
    if src is None:
        src = get_source_by_delimiter(delim)
        if src is None or not src["enabled"]:
            # 등록 목록이 바뀐 경우 등 → 되묻기로 안전 폴백
            return {"needs_source": True, "sources": _source_choices()}

    # A: '목록/종류' 질의는 Claude 호출 없이 결정적 카탈로그로 즉답한다(비용 0·정확).
    # 단, 특정 문서/테이블 이름을 지칭하면 내용 질의이므로 즉답하지 않는다(오탐 방지).
    if _is_inventory_query(stripped) and not _mentions_specific_item(src, stripped):
        answer, inv_sources = _build_inventory_answer(src)
        if answer:
            return {
                "needs_source": False,
                "direct_answer": answer,
                "question": stripped,
                "delimiter": delim,
                "category": src["label"],
                "files": [],
                "sources": inv_sources,
                "intent": "inventory",
            }

    # 주 출처 + 고정 연계 출처들의 문서를 한 번에 모은다(연계 없으면 주 출처 단독).
    documents, used_files, linked_labels, scan_meta = _gather_with_links(src, stripped)

    # 주 출처가 '규정'(pdf_index)인데 주·연계 어디서도 못 건졌으면
    # Claude 호출 없이 규정 제목 후보를 바로 안내한다(기존 결정적 UX 유지).
    if not used_files and src.get("kind") == "pdf_index":
        titles = list_titles(Path(src["root_path"]))
        bullets = "\n".join(f"- {t}" for t in titles)
        answer = (
            "## 어느 규정인지 좁혀주세요\n"
            "질문과 제목이 일치하는 규정을 찾지 못했습니다. "
            "아래 목록에서 **규정 이름을 질문에 포함**해 다시 물어봐 주세요. "
            f"예) `{delim} 학칙 수업연한`\n\n"
            f"### 규정 제목 ({len(titles)}개)\n{bullets}"
        )
        return {
            "needs_source": False,
            "direct_answer": answer,
            "question": stripped,
            "delimiter": delim,
            "category": src["label"],
            "files": [],
            "sources": [],
            "intent": "content",
        }

    # 업무영역 표기에 실제 기여한 연계 출처를 덧붙여 질의 범위를 드러낸다.
    category = src["label"]
    if linked_labels:
        category += " (+" + ", ".join(dict.fromkeys(linked_labels)) + ")"
    # D-1: 대형 폴더로 '파일명 기반 축소 검색'이 적용됐고 dev_view가 켜졌으면
    # 응답(업무영역 표기)에 그 사실을 드러내 조용한 품질 저하를 알린다.
    if dev_view and scan_meta.get("reduced_scan"):
        category += " ⚠️파일명 기반 축소 검색 적용"

    session_summary = _build_session_summary(session_id)
    catalog = _build_catalog(src)   # B: 목록/종류 질의 안전망 + 라우팅 규칙용 경량 목차
    sources = _sources_from_used(used_files)   # S-0: 실제 포함 출처(인용 닫힌 집합)
    prompt = build_answer_prompt(category, session_summary, documents, stripped,
                                 dev_view, catalog=catalog,
                                 cite_names=[s["path"] for s in sources])
    return {
        "needs_source": False,
        "question": stripped,
        "delimiter": delim,
        "category": category,
        "files": used_files,
        "prompt": prompt,
        "sources": sources,
        "intent": "content",
    }


# 답변 말미의 근거 표기 줄을 찾는다("근거 문서:" / "근거문서:" / "출처:").
_CITE_LINE_RE = re.compile(r"^(\s*(?:근거\s*문서|출처)\s*[:：]\s*)(.+)$")


def verify_citations(answer: str, sources: list[dict]) -> tuple[str, list[str]]:
    """모델이 적은 '근거 문서:' 줄을 실제 제공 출처 집합과 대조한다(F-1.3).
    집합에 없는 출처명만 그 줄에서 제거하고(본문은 절대 건드리지 않음),
    제거한 이름 목록을 함께 반환한다. 파싱 실패 시 원문 그대로 반환(안전)."""
    if not answer or not sources:
        return answer, []
    valid = []
    for s in sources:
        for k in ("path", "title"):
            v = (s.get(k) or "").strip()
            if v:
                valid.append(v)
    if not valid:
        return answer, []

    def _ok(name: str) -> bool:
        base = name.rsplit("/", 1)[-1]
        for v in valid:
            vbase = v.rsplit("/", 1)[-1]
            if name in v or v in name or base and base in v or vbase and vbase in name:
                return True
        return False

    try:
        lines = answer.splitlines()
        removed: list[str] = []
        for i in range(len(lines) - 1, -1, -1):
            m = _CITE_LINE_RE.match(lines[i])
            if not m:
                continue
            head, body = m.group(1), m.group(2)
            # 경로에 '/'가 들어가므로 슬래시로는 자르지 않는다(쉼표/가운뎃점만).
            names = [n.strip().strip("`").strip() for n in re.split(r"[,，、]| · ", body)]
            names = [n for n in names if n]
            kept = [n for n in names if _ok(n)]
            removed = [n for n in names if not _ok(n)]
            if removed:
                if kept:
                    lines[i] = head + ", ".join(kept)
                else:
                    lines[i] = head + "(제공된 출처에서 확인된 근거 없음)"
            break  # 마지막 근거 줄 하나만 처리
        if removed:
            return "\n".join(lines), removed
    except Exception:
        pass
    return answer, []


def persist_ask(session_id, question, answer, used_files, now_iso,
                elapsed_ms, category_label, source_delimiter=None,
                usage=None, user_id=None) -> int | None:
    """본문은 DB에만, 운영 로그에는 메타데이터만 남긴다.
    usage(A-5): 실토큰 dict가 주어지면 assistant 행과 운영 로그에 함께 기록한다.
    반환(S-0): 저장한 assistant 메시지의 id(피드백/출처 연동용)."""
    if user_id is not None and not ensure_session(session_id, now_iso, user_id):
        raise PermissionError("FORBIDDEN")
    if source_delimiter:
        set_session_source(session_id, source_delimiter)
    save_message(session_id, "user", question, "", now_iso)
    message_id = save_message(session_id, "assistant", answer,
                              ",".join(used_files), now_iso, usage=usage)
    log_query(category_label, used_files, elapsed_ms, "OK", usage=usage)
    return message_id


def handle_ask(question: str, session_id: str, now_iso: str,
               source_id: int | None = None, dev_view: bool = False,
               user_id: int | None = None) -> dict:
    """
    질문 1건 처리(비스트리밍). 동기 함수(블로킹 subprocess 포함)이므로
    호출부에서 threadpool로 실행한다.
    반환:
      - {"needs_source": True, "sources": [...]}  또는
      - {"needs_source": False, "category","files","answer"}
    예외: ValueError(코드) / RuntimeError(코드) / PermissionError
    """
    start = time.monotonic()
    prep = prepare_ask(question, session_id, source_id, dev_view)
    if prep["needs_source"]:
        return {"needs_source": True, "sources": prep["sources"]}

    # 규정 제목 미매칭 등 → Claude 호출 없이 결정적 안내를 그대로 반환
    if prep.get("direct_answer"):
        persist_ask(session_id, prep["question"], prep["direct_answer"], [],
                    now_iso, 0, prep["category"], prep["delimiter"],
                    user_id=user_id)
        return {
            "needs_source": False,
            "category": prep["category"],
            "files": [],
            "answer": prep["direct_answer"],
        }

    answer = run_claude(prep["prompt"])
    elapsed_ms = int((time.monotonic() - start) * 1000)
    persist_ask(session_id, prep["question"], answer, prep["files"],
                now_iso, elapsed_ms, prep["category"], prep["delimiter"],
                user_id=user_id)
    return {
        "needs_source": False,
        "category": prep["category"],
        "files": prep["files"],
        "answer": answer,
    }
