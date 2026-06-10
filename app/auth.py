# app/auth.py
"""
인증/인가 핵심.
- 비밀번호는 PBKDF2-HMAC-SHA256으로만 저장한다(평문/가역 저장 금지). 신규 의존성 없음.
- 세션은 랜덤 토큰을 발급해 DB(auth_sessions)에 저장하고 httponly 쿠키로 내려준다.
- 역할(role): 'admin' | 'user'. 폴더/사용자 관리는 admin만.
모든 SQL은 database.py의 파라미터 바인딩 함수만 거친다(여기서 직접 SQL 조립 안 함).
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

# 쿠키 이름과 세션 유효시간(시간). 로컬 전용이라 secure 플래그는 끄되 httponly는 유지.
SESSION_COOKIE = "sid"
SESSION_TTL_HOURS = 12

# PBKDF2 파라미터(저장 형식: pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex>)
_PBKDF2_ITERS = 200_000
_ALGO = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    """평문 비밀번호 → 저장용 해시 문자열. 매 호출 새 솔트를 쓴다."""
    if not isinstance(password, str) or not password:
        raise ValueError("EMPTY_PASSWORD")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS)
    return f"{_ALGO}${_PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """평문과 저장 해시를 상수시간 비교한다. 형식이 깨졌으면 False."""
    if not isinstance(password, str) or not isinstance(stored, str):
        return False
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split("$", 3)
        if algo != _ALGO:
            return False
        iters = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    return hmac.compare_digest(dk, expected)


def new_session_token() -> str:
    """추측 불가한 세션 토큰."""
    return secrets.token_urlsafe(32)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def session_expiry_iso() -> str:
    return (now_utc() + timedelta(hours=SESSION_TTL_HOURS)).isoformat()


def is_expired(expires_at_iso: str) -> bool:
    """저장된 만료 ISO 문자열이 현재보다 과거면 True(파싱 실패 시 만료로 간주)."""
    try:
        exp = datetime.fromisoformat(expires_at_iso)
    except (ValueError, TypeError):
        return True
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return now_utc() >= exp


def client_ip(request) -> str:
    """요청 클라이언트 IP. 로컬 전용이라 보통 127.0.0.1이지만 그대로 기록한다."""
    client = getattr(request, "client", None)
    return (client.host if client else None) or "unknown"
