# app/config.py
"""보안 관련 상수 집중. 하드코딩 대신 이 파일만 거친다."""
from pathlib import Path
import os

# 프로젝트 루트 (이 파일 기준 두 단계 위). 상대경로 기본값을 cwd가 아닌
# 프로젝트 루트에 고정해 실행 위치에 따른 경로 흔들림을 방지한다.
BASE_DIR = Path(__file__).resolve().parent.parent

# 업무 문서 루트 (이 폴더 밖은 절대 접근 불가)
KNOWLEDGE_DIR = Path(os.environ.get("KNOWLEDGE_DIR", BASE_DIR / "knowledge")).resolve()

# 허용 확장자 (내장 knowledge 기본 출처)
ALLOWED_SUFFIX = ".md"

# 등록 가능한 출처 폴더에서 허용하는 '텍스트형' 확장자 화이트리스트.
# 사용자가 폴더를 등록할 때 이 집합의 부분집합만 고를 수 있다(임의 확장자 차단).
# xlsx/hwp/pdf 등 바이너리는 텍스트 추출기가 필요하므로 1차 범위에서 제외.
TEXT_SUFFIXES = {".md", ".txt", ".csv", ".sql", ".pdf"}
# 위 중 .pdf 는 텍스트가 아니라 pypdf로 '본문 추출'해서 읽는다(아래 read 단계에서 처리).

# 출처(폴더) 등록 제한
MAX_SOURCES = 50          # 등록 가능한 최대 폴더 수
MAX_DELIM_LEN = 24        # 구분자 최대 길이 (@ 포함)
MAX_LABEL_LEN = 60        # 라벨 최대 길이
MAX_ROOT_PATH_LEN = 400   # 폴더 경로 최대 길이
MAX_DIVISION_LEN = 20     # 분기 최대 길이 (자유 입력)

# 내장 knowledge 폴더에 대응하는 기본 출처의 구분자/라벨
DEFAULT_SOURCE_DELIM = "@지식"
DEFAULT_SOURCE_LABEL = "지식"

# --- 규정(원격 PDF 목록) 출처 ---
# knowledge/학사/규정/*.md 의 PDF 링크를 질의 시 실시간으로 받아 분석한다.
REG_SOURCE_DELIM = "@규정"
REG_SOURCE_LABEL = "규정"
REG_SOURCE_SUBDIR = "규정"            # KNOWLEDGE_DIR 하위, 목록 .md 가 있는 폴더

# PDF 페처 보안/리소스 제한
PDF_FETCH_ALLOWLIST = {"www.kosin.ac.kr"}   # 이 호스트만 다운로드 허용(SSRF 방어)
PDF_FETCH_TIMEOUT = 20                        # 단일 PDF 다운로드 타임아웃(초)
MAX_PDF_BYTES = 20 * 1024 * 1024             # 단일 PDF 최대 크기(20MB)
MAX_PDF_FETCH = 3                            # 한 질의에서 받을 최대 PDF 수
MAX_PDF_TEXT_CHARS = 60 * 1024              # 단일 PDF 추출 텍스트 상한(문자)

def _env_int(name: str, default: int) -> int:
    """환경변수에서 양의 정수를 읽되, 없거나 잘못되면 기본값을 쓴다."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


# 입력/리소스 제한
MAX_QUESTION_LEN = 2000                  # 질문 최대 글자 수
MAX_FILE_BYTES = 512 * 1024              # 단일 md 파일 최대 크기 (512KB)
# Claude에 넘길 문서 총량 상한. 한글은 토큰이 추정보다 높게 나올 수 있어 보수적으로
# 낮춘 기본값(128KB)을 쓰고, 운영에서 A-5 실토큰 계측으로 재조정한다. (A-3)
MAX_TOTAL_CONTEXT_BYTES = _env_int("MAX_TOTAL_CONTEXT_BYTES", 128 * 1024)
MAX_FILES_PER_QUERY = 8                  # '한 출처'에서 참조할 최대 문서 수
# 한 질의에서 주출처+연계출처를 '합산'한 전역 문서 수 상한. 연계출처가 있어도
# 전체 누적이 이 값을 넘지 않게 한다(주출처 우선). (A-2)
MAX_FILES_TOTAL = _env_int("MAX_FILES_TOTAL", 12)
# 연계출처가 굶지 않도록 출처별로 보장하는 최소 컨텍스트 예산(바이트). 주출처가
# 우선권을 갖되, 뒤 출처에 이 예산을 남겨둔다. 배정이 0이면 스캔을 건너뛴다. (A-4)
MIN_SOURCE_CONTEXT_BYTES = _env_int("MIN_SOURCE_CONTEXT_BYTES", 8 * 1024)
CLAUDE_TIMEOUT_SEC = 90                  # CLI 실행 타임아웃

# 서버 바인딩.
# LAN 접속을 위해 0.0.0.0(모든 인터페이스)로 바인딩한다. 단, 이는 신뢰된 사내/가정
# LAN 전용이며 인터넷에 직접 공개하지 않는다(HTTPS·역방향 프록시 없이는 위험).
HOST = "0.0.0.0"
PORT = 8999
# Host 헤더 검증(DNS Rebinding 차단)에 허용할 "호스트:포트" 집합.
# localhost + 이 PC의 LAN IP. IP가 바뀌거나 다른 접속 주소가 필요하면
# 여기에 추가하거나 EXTRA_ALLOWED_HOSTS 환경변수(콤마 구분)로 더한다.
ALLOWED_HOSTS = {
    "127.0.0.1:8999", "localhost:8999",
    "192.168.132.216:8999",
}
ALLOWED_HOSTS |= {
    h.strip() for h in os.environ.get("EXTRA_ALLOWED_HOSTS", "").split(",") if h.strip()
}

# CLI 실행 시 상속할 환경변수 화이트리스트
ENV_WHITELIST = {"PATH", "HOME", "USERPROFILE", "SystemRoot", "ANTHROPIC_API_KEY", "LANG"}
# D-3: Windows에서는 CLI(node)가 설정 디렉터리를 찾을 때 APPDATA/LOCALAPPDATA가
# 필요할 수 있다. 최소 env 원칙은 유지하되 Windows에서만 두 키를 추가한다.
if os.name == "nt":
    ENV_WHITELIST |= {"APPDATA", "LOCALAPPDATA"}

# CLI 작업 디렉터리 (knowledge 폴더가 아닌 격리된 빈 폴더)
CLAUDE_WORKDIR = Path(os.environ.get("CLAUDE_WORKDIR", BASE_DIR / ".claude_sandbox")).resolve()

# @규정 선택(가벼운 분류성 작업)에 쓸 경량 모델 별칭. 본 답변은 기본 모델을 쓴다. (A-1)
# CLI가 받는 별칭('haiku'/'sonnet'/'opus') 또는 풀네임. 빈 값이면 기본 모델 사용.
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_EFFORT = os.environ.get("CLAUDE_EFFORT", "high")
REG_SELECT_MODEL = os.environ.get("REG_SELECT_MODEL", "haiku")

# Claude CLI 실행 파일 경로(선택). 미지정 시 PATH에서 해석한다.
# Windows에서는 npm 셰임(claude.cmd)이라 PATHEXT 해석이 필요하므로
# claude_runner가 shutil.which로 절대경로를 찾는다. 명시하려면 이 변수를 설정.
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "")

# 데이터/로그/정적 파일 경로
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data")).resolve()
DB_PATH = DATA_DIR / "app.db"
LOG_DIR = DATA_DIR / "logs"
STATIC_DIR = BASE_DIR / "static"

# 규정 PDF 추출 텍스트 캐시 폴더 (재질의 시 다운로드 생략)
PDF_CACHE_DIR = DATA_DIR / "pdf_cache"
