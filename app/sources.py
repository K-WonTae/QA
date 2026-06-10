# app/sources.py
"""
질의 대상 폴더(출처)의 등록 검증과 질문 라우팅 헬퍼.

신뢰 경계:
- 폴더 '등록'은 로컬 운영자(본인)만 수행하는 신뢰 동작이다.
  앱은 127.0.0.1 바인딩 + CSRF 토큰으로 잠겨 있으므로, 운영자가
  자신의 폴더를 고르는 것은 위협이 아니다.
- 그래도 입력은 항상 검증한다: 경로는 실재하는 디렉터리여야 하고,
  확장자는 TEXT_SUFFIXES 화이트리스트의 부분집합만 허용한다.
"""
import re
from pathlib import Path

from app.config import (
    TEXT_SUFFIXES,
    MAX_DELIM_LEN,
    MAX_LABEL_LEN,
    MAX_ROOT_PATH_LEN,
    MAX_DIVISION_LEN,
)

# 구분자: '@' 로 시작, 이후 영문/숫자/밑줄/하이픈/한글 1~. 공백·경로문자 불가.
_DELIM_RE = re.compile(r"^@[\w가-힣\-]{1,}$", re.UNICODE)

# 분기: 한글/영문/숫자/공백 및 일부 기호만(표시·필터용). 자유 입력.
_DIVISION_RE = re.compile(r"^[\w가-힣 ()._\-]{1,}$", re.UNICODE)

# 등록 자체를 막을 위험 경로(부분 경로 매칭, 대소문자 무시).
# 시스템/프로그램 디렉터리를 통째로 출처로 거는 실수를 방지한다.
_BLOCKED_ROOT_PARTS = (
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "system32",
)

DIVISIONS = ("학사", "행정")


class SourceError(ValueError):
    """등록 검증 실패. 메시지는 사용자에게 그대로 노출 가능한 한국어."""


def validate_delimiter(raw) -> str:
    if not isinstance(raw, str):
        raise SourceError("구분자가 올바르지 않습니다.")
    delim = raw.strip()
    if not delim.startswith("@"):
        delim = "@" + delim
    if len(delim) > MAX_DELIM_LEN:
        raise SourceError("구분자가 너무 깁니다.")
    if not _DELIM_RE.match(delim):
        raise SourceError("구분자는 '@' 뒤에 공백 없이 영문·숫자·한글·밑줄(_)·하이픈(-)만 쓸 수 있습니다.")
    return delim


def validate_label(raw) -> str:
    if not isinstance(raw, str):
        raise SourceError("이름이 올바르지 않습니다.")
    label = raw.strip()
    if not label:
        raise SourceError("이름을 입력해주세요.")
    if len(label) > MAX_LABEL_LEN:
        raise SourceError("이름이 너무 깁니다.")
    return label


def validate_division(raw) -> str:
    """분기는 자유 입력. 비우면 기본 '학사'. 표시·필터용이라 위험문자만 막는다."""
    if not isinstance(raw, str):
        return DIVISIONS[0]
    division = raw.strip()
    if not division:
        return DIVISIONS[0]
    if len(division) > MAX_DIVISION_LEN:
        raise SourceError("분기가 너무 깁니다.")
    if not _DIVISION_RE.match(division):
        raise SourceError("분기는 한글·영문·숫자·공백만 쓸 수 있습니다.")
    return division


def validate_suffixes(raw) -> str:
    """
    리스트(["md","txt"]) 또는 쉼표 문자열을 받아 TEXT_SUFFIXES 부분집합으로
    정규화한다. 반환은 정렬된 쉼표 문자열(예: ".csv,.md").
    """
    if isinstance(raw, str):
        items = [p for p in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise SourceError("확장자 형식이 올바르지 않습니다.")

    norm: set[str] = set()
    for it in items:
        if not isinstance(it, str):
            continue
        s = it.strip().lower()
        if not s:
            continue
        if not s.startswith("."):
            s = "." + s
        if s not in TEXT_SUFFIXES:
            raise SourceError(f"허용되지 않은 확장자입니다: {s}")
        norm.add(s)
    if not norm:
        raise SourceError("확장자를 하나 이상 선택해주세요.")
    return ",".join(sorted(norm))


def parse_suffix_set(stored: str) -> set[str]:
    """DB에 저장된 쉼표 문자열을 확장자 집합으로. 화이트리스트 교집합만 남긴다."""
    out: set[str] = set()
    for s in (stored or "").split(","):
        s = s.strip().lower()
        if s in TEXT_SUFFIXES:
            out.add(s)
    return out


def validate_root_path(raw) -> str:
    """
    폴더 경로 검증: 실재하는 디렉터리여야 하고, 심볼릭 링크/드라이브 루트/
    시스템 디렉터리는 거부한다. 반환은 정규화된 절대경로 문자열.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise SourceError("폴더 경로를 입력해주세요.")
    if len(raw) > MAX_ROOT_PATH_LEN:
        raise SourceError("폴더 경로가 너무 깁니다.")
    try:
        p = Path(raw.strip())
        resolved = p.resolve()
    except (OSError, ValueError):
        raise SourceError("폴더 경로를 해석할 수 없습니다.")

    if resolved.is_symlink():
        raise SourceError("심볼릭 링크는 등록할 수 없습니다.")
    if not resolved.exists() or not resolved.is_dir():
        raise SourceError("존재하는 폴더가 아닙니다.")
    # 드라이브/파일시스템 루트 통째 등록 방지 (예: C:\ , \\ )
    if resolved == Path(resolved.anchor):
        raise SourceError("드라이브 최상위 폴더는 등록할 수 없습니다.")
    low = resolved.as_posix().lower()
    for blocked in _BLOCKED_ROOT_PARTS:
        if f"/{blocked}/" in low + "/" or low.endswith(f"/{blocked}"):
            raise SourceError("시스템 폴더는 등록할 수 없습니다.")
    return str(resolved)


def parse_delimiter(question: str, known_delims: set[str]) -> tuple[str | None, str]:
    """
    질문 맨 앞 토큰이 '등록된' 구분자면 (구분자, 나머지질문)을 반환.
    아니면 (None, 원본질문). 구분자만 있고 질문이 없으면 나머지는 "".
    """
    stripped = question.lstrip()
    parts = stripped.split(None, 1)
    if not parts:
        return None, question
    first = parts[0]
    if first in known_delims:
        rest = parts[1] if len(parts) > 1 else ""
        return first, rest.strip()
    return None, question
