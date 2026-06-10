# app/logging_conf.py
"""
로깅 정책.
- 일반 로그: 시각, 업무영역, 참조 '파일명', 처리 시간, 결과 코드만.
- 절대 남기지 말 것: 질문 전문, 답변 전문, 문서 본문, 환경변수, stderr 원문.
- stderr/예외 전문은 별도 디버그 로거(파일)로 분리하고 권한을 제한한다.
"""
import logging
import sys

from app.config import LOG_DIR

# 일반(운영) 로거 ----------------------------------------------------------
logger = logging.getLogger("work_md_qa")

# 디버그(민감) 로거: stderr 원문/스택트레이스 전용, 별도 파일 ----------------
debug_logger = logging.getLogger("work_md_qa.debug")
debug_logger.propagate = False  # 일반/콘솔로 새어 나가지 않게 격리

_configured = False


def _lock_down(path):
    """디버그 로그 파일을 소유자만 읽기/쓰기 가능하도록 제한 (11장)."""
    import os
    import stat
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except (PermissionError, NotImplementedError, FileNotFoundError, OSError):
        # Windows는 NTFS ACL이 별도이므로 실패해도 치명적이지 않음. 안내만.
        pass


def setup_logging() -> None:
    """앱 시작 시 1회 호출."""
    global _configured
    if _configured:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    # 운영 로그
    app_handler = logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8")
    app_handler.setFormatter(fmt)
    logger.setLevel(logging.INFO)
    logger.addHandler(app_handler)

    # 콘솔에도 운영 로그만 (개발 편의)
    # C-1: Windows 콘솔 기본 인코딩(cp949)으로 한글이 깨진다. StreamHandler()의 기본
    # 스트림은 stderr 이므로 stderr(및 안전하게 stdout)를 utf-8로 재구성한다.
    # 파일 핸들러는 이미 utf-8이라 건드리지 않는다.
    for _stream in (sys.stderr, sys.stdout):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    # 디버그(민감) 로그 — 별도 파일, 권한 제한
    debug_path = LOG_DIR / "debug.log"
    debug_handler = logging.FileHandler(debug_path, encoding="utf-8")
    debug_handler.setFormatter(fmt)
    debug_logger.setLevel(logging.DEBUG)
    debug_logger.addHandler(debug_handler)
    _lock_down(debug_path)

    _configured = True


def log_query(category: str, files: list[str], elapsed_ms: int, status: str,
              usage: dict | None = None) -> None:
    """운영 로그: 본문 없이 메타데이터만. usage(A-5)가 있으면 실토큰도 함께 남긴다."""
    u = usage or {}
    logger.info(
        "category=%s files=%s elapsed=%dms status=%s "
        "in_tok=%s out_tok=%s cache_read=%s cache_write=%s model=%s",
        category, ",".join(files), elapsed_ms, status,
        u.get("input_tokens"), u.get("output_tokens"),
        u.get("cache_read"), u.get("cache_write"), u.get("model"),
    )


def log_internal_error(detail: str) -> None:
    """
    stderr 원문/예외 전문은 사용자에게 노출하지 않고 이 디버그 로거에만 남긴다.
    claude_runner._log_internal_error 등 내부에서만 호출.
    """
    debug_logger.error("internal_error: %s", detail)
