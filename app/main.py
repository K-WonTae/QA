# app/main.py
"""
FastAPI 앱.
- 127.0.0.1 바인딩 고정 (실행: uvicorn app.main:app --host 127.0.0.1 --port 8000)
- Host 헤더 검증 (DNS Rebinding 차단)
- CSRF 토큰 (상태 변경 POST 보호)
- 시작 시 파일 화이트리스트/카테고리/DB/로깅 초기화
"""
import json
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import (
    JSONResponse, HTMLResponse, PlainTextResponse, StreamingResponse,
    FileResponse, RedirectResponse,
)
from fastapi.concurrency import run_in_threadpool

from app.config import (
    ALLOWED_HOSTS, STATIC_DIR, CLAUDE_WORKDIR,
    KNOWLEDGE_DIR, ALLOWED_SUFFIX, MAX_SOURCES,
    DEFAULT_SOURCE_DELIM, DEFAULT_SOURCE_LABEL,
    REG_SOURCE_DELIM, REG_SOURCE_LABEL, REG_SOURCE_SUBDIR,
    DATA_DIR, PDF_FETCH_ALLOWLIST,
)
from app.database import (
    init_db, ensure_session, seed_default_source,
    list_sources, add_source, update_source, delete_source, count_sources,
    get_source_by_id, get_source_by_delimiter, get_meta, set_meta,
    set_links, get_all_link_ids,
    list_conversations, get_conversation, delete_conversation,
    message_exists, save_feedback,
    seed_admin_if_empty, get_user_by_username, get_user_by_id,
    create_user, list_users, count_admins, update_user_fields,
    set_user_password, delete_user, delete_user_sessions,
    create_auth_session, get_auth_session, delete_auth_session,
    purge_expired_sessions, record_login, list_login_history,
    list_login_ips, delete_sessions_by_ip,
    add_ip_block, remove_ip_block, list_ip_blocks,
)
from app import auth
from app.sources import (
    SourceError, validate_delimiter, validate_label,
    validate_suffixes, validate_root_path, validate_division,
    parse_suffix_set,
)
from app.security import build_allowed_files, resolve_safe
from app.pdf_source import parse_index
from app.logging_conf import setup_logging, _lock_down
from app.router import (
    handle_ask, prepare_ask, persist_ask, error_message, verify_citations,
)
from app.claude_runner import run_claude_stream, _terminate_tree

# 진행 중인 스트림의 CLI 프로세스 등록부 {stream_id: Popen}.
# Esc(취소) 요청이 오면 여기서 찾아 프로세스 트리를 종료한다.
ACTIVE_PROCS: dict = {}


def _load_or_create_app_token() -> str:
    """
    CSRF 토큰을 재시작에도 유지되게 영속화한다(C-4).
    우선순위: 1) 환경변수 APP_CSRF_TOKEN  2) data/csrf_token 파일  3) 새로 생성·저장.
    파일은 소유자만 읽기/쓰기로 제한한다. 영속화 실패 시에도 그 회차 토큰은 동작한다.
    """
    env = os.environ.get("APP_CSRF_TOKEN")
    if env:
        return env
    token_path = DATA_DIR / "csrf_token"
    try:
        if token_path.is_file():
            tok = token_path.read_text(encoding="utf-8").strip()
            if tok:
                return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(32)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        token_path.write_text(tok, encoding="utf-8")
        _lock_down(token_path)
    except OSError:
        pass
    return tok


# CSRF 토큰: 재시작에도 유지(영속화). 외부 노출 금지.
APP_TOKEN = _load_or_create_app_token()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    init_db()
    _reload_blocklist()
    CLAUDE_WORKDIR.mkdir(parents=True, exist_ok=True)
    # 내장 출처(@지식·@규정)는 '최초 실행 시 1회만' 등록한다.
    # 이후 사용자가 자유롭게 수정/삭제할 수 있고, 삭제해도 다시 생기지 않는다.
    if not get_meta("defaults_seeded"):
        academic_knowledge_dir = KNOWLEDGE_DIR / "학사"
        seed_default_source(
            DEFAULT_SOURCE_DELIM, DEFAULT_SOURCE_LABEL,
            str(academic_knowledge_dir), ALLOWED_SUFFIX, _now_iso(),
            division="학사",
        )
        seed_default_source(
            REG_SOURCE_DELIM, REG_SOURCE_LABEL,
            str(academic_knowledge_dir / REG_SOURCE_SUBDIR), ALLOWED_SUFFIX, _now_iso(),
            kind="pdf_index", division="학사",
        )
        set_meta("defaults_seeded", "1")
    # 최초 관리자(사용자가 한 명도 없을 때만 1회 생성).
    # 우선순위: 환경변수(INITIAL_ADMIN_USERNAME/PASSWORD)가 있으면 그 값으로,
    # 없으면 요구사항대로 admin/admin 으로 시드한다. 첫 로그인 후 비밀번호 변경을 안내한다.
    initial_admin_username = os.environ.get("INITIAL_ADMIN_USERNAME") or "admin"
    initial_admin_password = os.environ.get("INITIAL_ADMIN_PASSWORD") or "admin"
    if seed_admin_if_empty(
        initial_admin_username,
        auth.hash_password(initial_admin_password),
        _now_iso(),
    ):
        from app.logging_conf import logger
        logger.info(
            "seeded initial admin account (username=%s) — change the password after first login",
            initial_admin_username,
        )
    yield


app = FastAPI(lifespan=lifespan)


# 차단 IP 캐시(메모리). DB가 진실 원본이고, 변경 시 _reload_blocklist로 갱신한다.
_BLOCKED_IPS: set = set()
# 루프백은 절대 차단하지 않는다(서버 PC에서의 관리자 복구 경로 보장).
_LOOPBACK_IPS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _reload_blocklist() -> None:
    global _BLOCKED_IPS
    try:
        _BLOCKED_IPS = {b["ip"] for b in list_ip_blocks()}
    except Exception:
        _BLOCKED_IPS = set()


def _is_loopback(ip: str) -> bool:
    return ip in _LOOPBACK_IPS


@app.middleware("http")
async def host_guard(request: Request, call_next):
    """8.2 Host 헤더 검증(DNS Rebinding 차단) + 차단 IP 거부."""
    if request.headers.get("host") not in ALLOWED_HOSTS:
        return JSONResponse(status_code=400, content={"error": "BAD_HOST"})
    ip = auth.client_ip(request)
    if ip in _BLOCKED_IPS and not _is_loopback(ip):
        return JSONResponse(status_code=403, content={"error": "차단된 IP입니다. 관리자에게 문의하세요."})
    return await call_next(request)


def require_app_token(request: Request) -> None:
    """8.3 CSRF: 상태 변경 요청은 발급된 토큰 일치 시에만 처리."""
    if request.headers.get("x-app-token") != APP_TOKEN:
        raise PermissionError("CSRF_BLOCKED")


# ---- 인증/인가 ----

def current_user(request: Request) -> dict | None:
    """쿠키의 세션 토큰으로 현재 사용자를 해석한다.
    유효(미만료·활성 사용자)하면 {user_id, username, role}, 아니면 None.
    만료 세션은 즉시 정리한다."""
    token = request.cookies.get(auth.SESSION_COOKIE)
    if not token:
        return None
    sess = get_auth_session(token)
    if sess is None:
        return None
    if not sess.get("enabled") or auth.is_expired(sess["expires_at"]):
        delete_auth_session(token)
        return None
    return {"user_id": sess["user_id"], "username": sess["username"], "role": sess["role"]}


def require_user(request: Request) -> dict:
    """로그인 필요. 미인증이면 PermissionError('AUTH_REQUIRED')."""
    user = current_user(request)
    if user is None:
        raise PermissionError("AUTH_REQUIRED")
    return user


def require_admin(request: Request) -> dict:
    """관리자 권한 필요. 미인증/권한부족이면 PermissionError."""
    user = require_user(request)
    if user.get("role") != "admin":
        raise PermissionError("FORBIDDEN")
    return user


def _set_session_cookie(resp, token: str) -> None:
    """세션 쿠키 설정(httponly, samesite=lax). 로컬 http라 secure는 끈다."""
    resp.set_cookie(
        key=auth.SESSION_COOKIE, value=token, httponly=True,
        samesite="lax", max_age=auth.SESSION_TTL_HOURS * 3600, path="/",
    )


def _auth_error_response(code: str) -> JSONResponse:
    """인증/인가 PermissionError를 상태코드에 맞춰 응답으로 변환한다.
    AUTH_REQUIRED → 401, FORBIDDEN → 403, 그 외(CSRF 등) → 403."""
    status = 401 if code == "AUTH_REQUIRED" else 403
    return JSONResponse(status_code=status, content={"error": error_message(code)})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _parse_ask_request(request: Request) -> dict:
    """질의 요청의 공통 전처리: CSRF 검증 → JSON 파싱 → 파라미터 정규화 → 세션 보장.
    /api/ask 와 /api/ask/stream 이 공유한다(C-2 공용화).
    예외: PermissionError(CSRF 차단) / ValueError('INVALID_INPUT').
    반환: {question, session_id, source_id, dev_view, now}
    """
    require_app_token(request)  # 실패 시 PermissionError(CSRF_BLOCKED)
    require_user(request)       # 실패 시 PermissionError(AUTH_REQUIRED)
    try:
        payload = await request.json()
    except Exception:
        raise ValueError("INVALID_INPUT")

    question = payload.get("question") if isinstance(payload, dict) else None
    session_id = payload.get("session_id") if isinstance(payload, dict) else None
    source_id = payload.get("source_id") if isinstance(payload, dict) else None
    if not isinstance(source_id, int):
        source_id = None
    dev_view = bool(payload.get("dev_view")) if isinstance(payload, dict) else False
    if not isinstance(session_id, str) or not session_id:
        session_id = uuid.uuid4().hex

    now = _now_iso()
    ensure_session(session_id, now)
    return {
        "question": question, "session_id": session_id,
        "source_id": source_id, "dev_view": dev_view, "now": now,
    }


def _render_page(name: str) -> HTMLResponse:
    """static HTML을 읽어 CSRF 토큰을 주입해 응답한다."""
    html = (STATIC_DIR / name).read_text(encoding="utf-8")
    html = html.replace("__APP_TOKEN__", APP_TOKEN)
    return HTMLResponse(content=html)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """로그인 페이지. 이미 로그인했으면 채팅으로 보낸다."""
    if current_user(request) is not None:
        return RedirectResponse("/", status_code=302)
    return _render_page("login.html")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """채팅 화면. 미로그인 시 로그인 페이지로 보낸다."""
    if current_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return _render_page("index.html")


@app.get("/sources", response_class=HTMLResponse)
async def sources_page(request: Request):
    """폴더(출처) 등록 관리 페이지 — 관리자 전용."""
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    if user.get("role") != "admin":
        return RedirectResponse("/", status_code=302)
    return _render_page("sources.html")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """사용자 관리 + 로그인 이력 페이지 — 관리자 전용."""
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    if user.get("role") != "admin":
        return RedirectResponse("/", status_code=302)
    return _render_page("admin.html")


# ---- 인증 API ----

@app.post("/api/login")
async def api_login(request: Request):
    """로그인. 성공 시 세션 쿠키 발급. 성공/실패 모두 이력에 IP·UA를 기록한다."""
    try:
        require_app_token(request)
    except PermissionError:
        return _auth_error_response("CSRF_BLOCKED")
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})
    username = payload.get("username") if isinstance(payload, dict) else None
    password = payload.get("password") if isinstance(payload, dict) else None
    if not isinstance(username, str) or not isinstance(password, str) \
            or not username.strip() or not password:
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})
    username = username.strip()

    ip = auth.client_ip(request)
    ua = (request.headers.get("user-agent") or "")[:300]
    now = _now_iso()

    user = await run_in_threadpool(get_user_by_username, username)
    ok = bool(user) and bool(user["enabled"]) and auth.verify_password(password, user["password_hash"])
    await run_in_threadpool(
        record_login, (user["id"] if user else None), username, ok, ip, ua, now
    )
    if not ok:
        # 사용자 존재 여부를 노출하지 않도록 단일 메시지.
        return JSONResponse(status_code=401, content={"error": "아이디 또는 비밀번호가 올바르지 않습니다."})

    token = auth.new_session_token()
    await run_in_threadpool(
        create_auth_session, token, user["id"], now, auth.session_expiry_iso(), ip
    )
    await run_in_threadpool(purge_expired_sessions, now)
    resp = JSONResponse(content={
        "ok": True,
        "user": {"username": user["username"], "role": user["role"]},
        "must_change_password": auth.verify_password("admin", user["password_hash"]),
    })
    _set_session_cookie(resp, token)
    return resp


@app.post("/api/logout")
async def api_logout(request: Request):
    """로그아웃. 세션을 무효화하고 쿠키를 지운다(CSRF 토큰 필요)."""
    try:
        require_app_token(request)
    except PermissionError:
        return _auth_error_response("CSRF_BLOCKED")
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        await run_in_threadpool(delete_auth_session, token)
    resp = JSONResponse(content={"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@app.get("/api/me")
async def api_me(request: Request):
    """현재 로그인 사용자 정보(헤더 표시·권한 분기용)."""
    user = current_user(request)
    if user is None:
        return _auth_error_response("AUTH_REQUIRED")
    return JSONResponse(content={"username": user["username"], "role": user["role"]})


@app.post("/api/change-password")
async def api_change_password(request: Request):
    """본인 비밀번호 변경. 현재 비밀번호 확인 후 교체하고 다른 세션을 모두 무효화한다."""
    try:
        require_app_token(request)
        user = require_user(request)
    except PermissionError as e:
        return _auth_error_response(str(e))
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})
    current_pw = payload.get("current_password") if isinstance(payload, dict) else None
    new_pw = payload.get("new_password") if isinstance(payload, dict) else None
    if not isinstance(current_pw, str) or not isinstance(new_pw, str) or len(new_pw) < 4:
        return JSONResponse(status_code=400, content={"error": "새 비밀번호는 4자 이상이어야 합니다."})

    row = await run_in_threadpool(get_user_by_id, user["user_id"])
    if row is None or not auth.verify_password(current_pw, row["password_hash"]):
        return JSONResponse(status_code=400, content={"error": "현재 비밀번호가 올바르지 않습니다."})

    await run_in_threadpool(set_user_password, user["user_id"], auth.hash_password(new_pw))
    # 다른 기기 세션은 모두 끊고, 현재 세션만 새로 발급한다.
    await run_in_threadpool(delete_user_sessions, user["user_id"])
    token = auth.new_session_token()
    now = _now_iso()
    await run_in_threadpool(
        create_auth_session, token, user["user_id"], now, auth.session_expiry_iso(),
        auth.client_ip(request),
    )
    resp = JSONResponse(content={"ok": True})
    _set_session_cookie(resp, token)
    return resp


# ---- 사용자 관리 API (관리자 전용) ----

def _user_public(u: dict) -> dict:
    """비밀번호 해시를 제외한 안전한 사용자 표현."""
    return {
        "id": u["id"], "username": u["username"], "role": u["role"],
        "enabled": bool(u["enabled"]), "created_at": u["created_at"],
        "created_by": u.get("created_by"),
    }


@app.get("/api/users")
async def api_list_users(request: Request):
    try:
        require_admin(request)
    except PermissionError as e:
        return _auth_error_response(str(e))
    rows = await run_in_threadpool(list_users)
    return JSONResponse(content={"users": [_user_public(u) for u in rows]})


@app.post("/api/users")
async def api_create_user(request: Request):
    try:
        require_app_token(request)
        admin = require_admin(request)
    except PermissionError as e:
        return _auth_error_response(str(e))
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})
    username = (payload.get("username") if isinstance(payload, dict) else None) or ""
    password = (payload.get("password") if isinstance(payload, dict) else None) or ""
    role = (payload.get("role") if isinstance(payload, dict) else None) or "user"
    username = username.strip() if isinstance(username, str) else ""
    if not username or not isinstance(password, str) or len(password) < 4:
        return JSONResponse(status_code=400, content={"error": "아이디와 4자 이상 비밀번호를 입력해주세요."})
    if not (3 <= len(username) <= 30) or not all(c.isalnum() or c in "._-" for c in username):
        return JSONResponse(status_code=400, content={"error": "아이디는 3~30자의 영문/숫자/._- 만 가능합니다."})
    if role not in ("admin", "user"):
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})

    try:
        await run_in_threadpool(
            create_user, username, auth.hash_password(password), role,
            _now_iso(), admin["username"],
        )
    except Exception:
        return JSONResponse(status_code=400, content={"error": "이미 존재하는 아이디입니다."})
    return JSONResponse(content={"ok": True})


@app.post("/api/users/update")
async def api_update_user(request: Request):
    """역할/활성 여부 또는 비밀번호 변경(관리자가 타 사용자 대상)."""
    try:
        require_app_token(request)
        admin = require_admin(request)
    except PermissionError as e:
        return _auth_error_response(str(e))
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})
    uid = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(uid, int) or isinstance(uid, bool):
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})
    target = await run_in_threadpool(get_user_by_id, uid)
    if target is None:
        return JSONResponse(status_code=400, content={"error": "존재하지 않는 사용자입니다."})

    # 비밀번호 재설정만 요청한 경우
    new_pw = payload.get("new_password") if isinstance(payload, dict) else None
    if new_pw is not None:
        if not isinstance(new_pw, str) or len(new_pw) < 4:
            return JSONResponse(status_code=400, content={"error": "새 비밀번호는 4자 이상이어야 합니다."})
        await run_in_threadpool(set_user_password, uid, auth.hash_password(new_pw))
        await run_in_threadpool(delete_user_sessions, uid)  # 기존 세션 무효화
        return JSONResponse(content={"ok": True})

    role = payload.get("role") if isinstance(payload, dict) else None
    enabled = payload.get("enabled") if isinstance(payload, dict) else None
    if role not in ("admin", "user") or not isinstance(enabled, bool):
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})

    # 마지막 관리자를 강등/비활성화하지 못하게 막는다(잠금 방지).
    losing_admin = (target["role"] == "admin" and bool(target["enabled"])
                    and (role != "admin" or not enabled))
    if losing_admin and await run_in_threadpool(count_admins) <= 1:
        return JSONResponse(status_code=400, content={"error": "마지막 관리자는 강등하거나 비활성화할 수 없습니다."})

    await run_in_threadpool(update_user_fields, uid, role, 1 if enabled else 0)
    if not enabled:
        await run_in_threadpool(delete_user_sessions, uid)  # 비활성화 시 즉시 로그아웃
    return JSONResponse(content={"ok": True})


@app.post("/api/users/delete")
async def api_delete_user(request: Request):
    try:
        require_app_token(request)
        admin = require_admin(request)
    except PermissionError as e:
        return _auth_error_response(str(e))
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    uid = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(uid, int) or isinstance(uid, bool):
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})
    if uid == admin["user_id"]:
        return JSONResponse(status_code=400, content={"error": "본인 계정은 삭제할 수 없습니다."})
    target = await run_in_threadpool(get_user_by_id, uid)
    if target is None:
        return JSONResponse(status_code=400, content={"error": "존재하지 않는 사용자입니다."})
    if target["role"] == "admin" and bool(target["enabled"]) and await run_in_threadpool(count_admins) <= 1:
        return JSONResponse(status_code=400, content={"error": "마지막 관리자는 삭제할 수 없습니다."})
    await run_in_threadpool(delete_user, uid)
    return JSONResponse(content={"ok": True})


@app.get("/api/login-history")
async def api_login_history(request: Request):
    try:
        require_admin(request)
    except PermissionError as e:
        return _auth_error_response(str(e))
    rows = await run_in_threadpool(list_login_history, 200)
    return JSONResponse(content={"history": rows})


# ---- IP 차단(블랙리스트) — 관리자 전용 ----

def _valid_ip_str(ip) -> str | None:
    """IP 문자열 가벼운 검증(IPv4/IPv6 형태의 문자만 허용). 통과 시 정규화된 문자열."""
    if not isinstance(ip, str):
        return None
    ip = ip.strip()
    if not ip or len(ip) > 45:
        return None
    if not all(c.isdigit() or c in "abcdefABCDEF.:" for c in ip):
        return None
    return ip


@app.get("/api/login-ips")
async def api_login_ips(request: Request):
    """로그인 이력에 등장한 IP별 집계 + 차단 상태. 차단만 된(이력 없는) IP도 함께 보인다."""
    try:
        require_admin(request)
    except PermissionError as e:
        return _auth_error_response(str(e))
    rows = await run_in_threadpool(list_login_ips)
    blocks = {b["ip"]: b for b in await run_in_threadpool(list_ip_blocks)}
    my_ip = auth.client_ip(request)
    out = []
    seen = set()
    for r in rows:
        ip = r["ip"]
        seen.add(ip)
        b = blocks.get(ip)
        out.append({
            "ip": ip, "attempts": r["attempts"], "successes": r["successes"],
            "fails": r["attempts"] - r["successes"], "last_at": r["last_at"],
            "usernames": r["usernames"], "blocked": b is not None,
            "reason": (b["reason"] if b else None),
            "is_self": ip == my_ip, "is_loopback": _is_loopback(ip),
        })
    # 이력에 없지만 수동으로 차단된 IP도 표에 포함
    for ip, b in blocks.items():
        if ip in seen:
            continue
        out.append({
            "ip": ip, "attempts": 0, "successes": 0, "fails": 0,
            "last_at": b["created_at"], "usernames": "", "blocked": True,
            "reason": b["reason"], "is_self": ip == my_ip, "is_loopback": _is_loopback(ip),
        })
    return JSONResponse(content={"ips": out, "my_ip": my_ip})


@app.post("/api/blacklist")
async def api_block_ip(request: Request):
    """IP를 차단한다. 본인 현재 IP·루프백은 차단할 수 없다(자기 잠금 방지)."""
    try:
        require_app_token(request)
        admin = require_admin(request)
    except PermissionError as e:
        return _auth_error_response(str(e))
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})
    ip = _valid_ip_str(payload.get("ip") if isinstance(payload, dict) else None)
    if ip is None:
        return JSONResponse(status_code=400, content={"error": "올바른 IP 주소를 입력해주세요."})
    if _is_loopback(ip):
        return JSONResponse(status_code=400, content={"error": "로컬(루프백) 주소는 차단할 수 없습니다."})
    if ip == auth.client_ip(request):
        return JSONResponse(status_code=400, content={"error": "현재 접속 중인 본인 IP는 차단할 수 없습니다."})
    reason = payload.get("reason") if isinstance(payload, dict) else None
    if reason is not None:
        if not isinstance(reason, str):
            return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})
        reason = reason.strip()[:200] or None

    await run_in_threadpool(add_ip_block, ip, reason, _now_iso(), admin["username"])
    await run_in_threadpool(delete_sessions_by_ip, ip)  # 차단 IP의 활성 세션 즉시 해제
    _reload_blocklist()
    return JSONResponse(content={"ok": True})


@app.post("/api/blacklist/delete")
async def api_unblock_ip(request: Request):
    """IP 차단을 해제한다."""
    try:
        require_app_token(request)
        require_admin(request)
    except PermissionError as e:
        return _auth_error_response(str(e))
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    ip = _valid_ip_str(payload.get("ip") if isinstance(payload, dict) else None)
    if ip is None:
        return JSONResponse(status_code=400, content={"error": "올바른 IP 주소를 입력해주세요."})
    await run_in_threadpool(remove_ip_block, ip)
    _reload_blocklist()
    return JSONResponse(content={"ok": True})


def _source_public(s: dict, linked_ids: list[int] | None = None) -> dict:
    """클라이언트에 내려줄 출처 표현(민감정보 없음)."""
    return {
        "id": s["id"],
        "delimiter": s["delimiter"],
        "label": s["label"],
        "root_path": s["root_path"],
        "suffixes": s["suffixes"],
        "division": s.get("division", "학사"),
        "kind": s.get("kind", "local"),
        "is_default": bool(s["is_default"]),
        "enabled": bool(s["enabled"]),
        "linked_ids": linked_ids or [],
    }


def _clean_linked_ids(raw, valid_ids: set, exclude_id=None) -> list[int]:
    """입력 linked_ids 를 '실재하는 출처 id'로만 정제한다(자기 자신·중복·불린 제거)."""
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for v in raw:
        if isinstance(v, bool):
            continue
        if isinstance(v, int) and v in valid_ids and v != exclude_id and v not in out:
            out.append(v)
    return out


@app.get("/api/sources")
async def api_list_sources(request: Request):
    # 채팅 드롭다운 구성에 필요하므로 로그인 사용자라면 누구나 조회 가능(관리는 별개).
    try:
        require_user(request)
    except PermissionError as e:
        return _auth_error_response(str(e))
    rows = await run_in_threadpool(list_sources, False)
    links = await run_in_threadpool(get_all_link_ids)
    return JSONResponse(content={
        "sources": [_source_public(s, links.get(s["id"], [])) for s in rows]
    })


@app.post("/api/sources")
async def api_add_source(request: Request):
    try:
        require_app_token(request)
        require_admin(request)
    except PermissionError as e:
        return _auth_error_response(str(e))
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})

    # 등록 검증(사용자 노출 가능한 한국어 메시지)
    try:
        delimiter = validate_delimiter(payload.get("delimiter"))
        label = validate_label(payload.get("label"))
        division = validate_division(payload.get("division"))
        root_path = validate_root_path(payload.get("root_path"))
        suffixes = validate_suffixes(payload.get("suffixes"))
    except SourceError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    # 개수 상한
    if await run_in_threadpool(count_sources) >= MAX_SOURCES:
        return JSONResponse(status_code=400, content={"error": "등록 가능한 폴더 수를 초과했습니다."})

    try:
        new_id = await run_in_threadpool(
            add_source, delimiter, label, root_path, suffixes, division, _now_iso()
        )
    except Exception:
        # UNIQUE 제약(중복 구분자) 등
        return JSONResponse(status_code=400, content={"error": "이미 사용 중인 구분자입니다."})

    # 연계 출처 등록(실재하는 출처 id로만 정제). 새 출처 자신은 자동 제외.
    existing_ids = {s["id"] for s in await run_in_threadpool(list_sources, False)}
    linked = _clean_linked_ids(payload.get("linked_ids"), existing_ids, exclude_id=new_id)
    await run_in_threadpool(set_links, new_id, linked)

    return JSONResponse(content={"ok": True})


@app.post("/api/sources/update")
async def api_update_source(request: Request):
    try:
        require_app_token(request)
        require_admin(request)
    except PermissionError as e:
        return _auth_error_response(str(e))
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})

    sid = payload.get("id")
    if not isinstance(sid, int):
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})
    if await run_in_threadpool(get_source_by_id, sid) is None:
        return JSONResponse(status_code=400, content={"error": "존재하지 않는 항목입니다."})

    try:
        delimiter = validate_delimiter(payload.get("delimiter"))
        label = validate_label(payload.get("label"))
        division = validate_division(payload.get("division"))
        root_path = validate_root_path(payload.get("root_path"))
        suffixes = validate_suffixes(payload.get("suffixes"))
    except SourceError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    try:
        await run_in_threadpool(
            update_source, sid, delimiter, label, root_path, suffixes, division
        )
    except Exception:
        return JSONResponse(status_code=400, content={"error": "이미 사용 중인 구분자입니다."})

    # 연계 출처 목록을 통째로 교체(실재 id로만 정제, 자기 자신 제외).
    existing_ids = {s["id"] for s in await run_in_threadpool(list_sources, False)}
    linked = _clean_linked_ids(payload.get("linked_ids"), existing_ids, exclude_id=sid)
    await run_in_threadpool(set_links, sid, linked)

    return JSONResponse(content={"ok": True})


@app.post("/api/sources/delete")
async def api_delete_source(request: Request):
    try:
        require_app_token(request)
        require_admin(request)
    except PermissionError as e:
        return _auth_error_response(str(e))
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    sid = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(sid, int):
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})
    ok = await run_in_threadpool(delete_source, sid)
    if not ok:
        return JSONResponse(status_code=400, content={"error": "존재하지 않는 항목입니다."})
    return JSONResponse(content={"ok": True})


@app.get("/api/history")
async def api_history_list(request: Request):
    """대화 이력 목록(최근순) — 로그인 필요."""
    try:
        require_user(request)
    except PermissionError as e:
        return _auth_error_response(str(e))
    rows = await run_in_threadpool(list_conversations, 100)
    return JSONResponse(content={"conversations": rows})


@app.get("/api/history/{session_id}")
async def api_history_get(request: Request, session_id: str):
    """한 대화의 메시지 전체 — 로그인 필요."""
    try:
        require_user(request)
    except PermissionError as e:
        return _auth_error_response(str(e))
    if not isinstance(session_id, str) or not session_id.strip():
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})
    msgs = await run_in_threadpool(get_conversation, session_id, 500)
    return JSONResponse(content={"session_id": session_id, "messages": msgs})


@app.post("/api/history/delete")
async def api_history_delete(request: Request):
    try:
        require_app_token(request)
        require_user(request)
    except PermissionError as e:
        return _auth_error_response(str(e))
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    sid = payload.get("session_id") if isinstance(payload, dict) else None
    if not isinstance(sid, str) or not sid.strip():
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})
    await run_in_threadpool(delete_conversation, sid)
    return JSONResponse(content={"ok": True})


@app.get("/api/health", response_class=PlainTextResponse)
async def health():
    return "ok"


@app.post("/api/ask")
async def ask(request: Request):
    # 공통 전처리(CSRF·인증·파싱·세션). 실패는 그대로 에러 응답으로 매핑.
    try:
        params = await _parse_ask_request(request)
    except PermissionError as e:
        return _auth_error_response(str(e))
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": error_message(str(e))})

    question = params["question"]
    session_id = params["session_id"]
    now = params["now"]

    try:
        result = await run_in_threadpool(
            handle_ask, question, session_id, now,
            params["source_id"], params["dev_view"],
        )
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": error_message(str(e))})
    except PermissionError:
        return JSONResponse(status_code=403, content={"error": error_message("PermissionError")})
    except RuntimeError as e:
        code = str(e)
        status = 504 if code == "CLAUDE_TIMEOUT" else 502
        return JSONResponse(status_code=status, content={"error": error_message(code)})
    except Exception:
        # 어떤 내부 예외도 원문/스택트레이스를 응답에 싣지 않는다.
        return JSONResponse(status_code=500, content={"error": error_message("INTERNAL")})

    # 구분자 없음 → 어느 폴더에 물을지 되묻는다.
    if result.get("needs_source"):
        return JSONResponse(content={
            "session_id": session_id,
            "needs_source": True,
            "sources": result["sources"],
        })

    return JSONResponse(content={
        "session_id": session_id,
        "category": result["category"],
        "files": result["files"],
        "answer": result["answer"],
    })


def _sse(event: str, data: dict) -> str:
    """SSE 프레임 1개. 한글이 깨지지 않도록 ensure_ascii=False."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/ask/stream")
async def ask_stream(request: Request):
    """답변을 토큰 단위로 흘려보내는 스트리밍 엔드포인트(SSE over POST)."""
    # 공통 전처리(CSRF·인증·파싱·세션). /api/ask 와 동일 헬퍼를 공유(C-2).
    try:
        params = await _parse_ask_request(request)
    except PermissionError as e:
        return _auth_error_response(str(e))
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": error_message(str(e))})

    question = params["question"]
    session_id = params["session_id"]
    source_id = params["source_id"]
    dev_view = params["dev_view"]
    now = params["now"]

    # CLI 호출 전 단계(검증/분류/문서선택/프롬프트)는 빠르므로 먼저 동기 처리.
    # 여기서 입력 오류면 스트림을 열기 전에 일반 JSON 에러로 응답한다.
    try:
        prep = await run_in_threadpool(prepare_ask, question, session_id, source_id, dev_view)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": error_message(str(e))})
    except Exception:
        return JSONResponse(status_code=500, content={"error": error_message("INTERNAL")})

    # 구분자 없음 → 스트림을 열지 않고 되묻기 응답(JSON)을 보낸다.
    if prep.get("needs_source"):
        return JSONResponse(content={
            "session_id": session_id,
            "needs_source": True,
            "sources": prep["sources"],
        })

    # 규정 제목 미매칭 등 결정적 안내 → Claude 없이 한 번에 흘려보낸다.
    if prep.get("direct_answer"):
        answer = prep["direct_answer"]

        def gen_direct():
            yield _sse("meta", {"category": prep["category"], "files": [], "stream_id": ""})
            yield _sse("delta", {"text": answer})
            message_id = None
            try:
                message_id = persist_ask(session_id, prep["question"], answer, [],
                                         now, 0, prep["category"], prep.get("delimiter"))
            except Exception:
                pass
            yield _sse("done", {
                "session_id": session_id,
                "message_id": message_id,
                "sources": prep.get("sources", []),
            })

        return StreamingResponse(
            gen_direct(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    stream_id = uuid.uuid4().hex

    def gen():
        # 1) 메타(업무영역/참조 문서 + 취소용 stream_id)를 먼저 보낸다.
        yield _sse("meta", {
            "category": prep["category"],
            "files": prep["files"],
            "stream_id": stream_id,
        })
        start = time.monotonic()
        collected = []
        usage: dict = {}   # A-5: stream-json에서 파싱한 실토큰이 채워진다.
        try:
            for chunk in run_claude_stream(
                prep["prompt"],
                on_start=lambda p: ACTIVE_PROCS.__setitem__(stream_id, p),
                usage_sink=usage,
            ):
                collected.append(chunk)
                yield _sse("delta", {"text": chunk})
        except RuntimeError as e:
            yield _sse("error", {"error": error_message(str(e))})
            return
        except Exception:
            yield _sse("error", {"error": error_message("INTERNAL")})
            return
        finally:
            ACTIVE_PROCS.pop(stream_id, None)
        answer = "".join(collected)
        # F-1.3: 모델이 적은 '근거 문서:' 줄에서 실제 제공 집합 밖 출처는 제거(본문 불변).
        answer, removed_cites = verify_citations(answer, prep.get("sources", []))
        elapsed_ms = int((time.monotonic() - start) * 1000)
        # 기록(본문은 DB에만, 운영 로그에는 메타데이터만 + 실토큰 usage)
        message_id = None
        try:
            message_id = persist_ask(session_id, prep["question"], answer, prep["files"],
                                     now, elapsed_ms, prep["category"], prep["delimiter"],
                                     usage=usage)
        except Exception:
            pass
        yield _sse("done", {
            "session_id": session_id,
            "message_id": message_id,
            "sources": prep.get("sources", []),
            "removed_citations": removed_cites,
        })

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/ask/cancel")
async def ask_cancel(request: Request):
    """Esc(중지) 요청: 해당 stream_id의 CLI 프로세스 트리를 즉시 종료한다."""
    try:
        require_app_token(request)
        require_user(request)
    except PermissionError as e:
        return _auth_error_response(str(e))
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    stream_id = payload.get("stream_id") if isinstance(payload, dict) else None
    proc = ACTIVE_PROCS.pop(stream_id, None) if stream_id else None
    if proc is not None:
        await run_in_threadpool(_terminate_tree, proc)
        return JSONResponse(content={"cancelled": True})
    return JSONResponse(content={"cancelled": False})


@app.post("/api/feedback")
async def feedback(request: Request):
    """답변 피드백 수집(F-3). 코멘트는 저장 전용 — 어떤 프롬프트로도 되먹이지 않는다."""
    try:
        require_app_token(request)
        require_user(request)
    except PermissionError as e:
        return _auth_error_response(str(e))
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})

    message_id = payload.get("message_id")
    rating = payload.get("rating")
    comment = payload.get("comment")
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})
    if rating not in ("up", "down"):
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})
    if comment is not None:
        if not isinstance(comment, str):
            return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})
        comment = comment.strip()[:500]   # 길이 제한·정제
        comment = comment or None

    # 실재하는 assistant 메시지에만 기록(임의 id 차단).
    if not await run_in_threadpool(message_exists, message_id):
        return JSONResponse(status_code=400, content={"error": "존재하지 않는 메시지입니다."})

    await run_in_threadpool(save_feedback, message_id, rating, comment, _now_iso())
    return JSONResponse(content={"ok": True})


@app.get("/api/source-file")
async def source_file(request: Request, source: str = "", path: str = ""):
    """참고 문서 다운로드. 반드시 security.py 화이트리스트 관문을 거친다.
    - 로컬 출처: resolve_safe 로 (root 안·심볼릭링크 아님·확장자 화이트리스트) 검증 후 파일 전송.
    - 규정(pdf_index): 제목→공개 URL 매핑 후 허용 호스트(https)만 리다이렉트(서버가 받지 않음).
    GET 읽기이며 Host 헤더 가드로 localhost 로만 제한된다(다른 GET 조회와 동일 수준).
    """
    try:
        require_user(request)
    except PermissionError as e:
        return _auth_error_response(str(e))
    if not source or not path:
        return JSONResponse(status_code=400, content={"error": error_message("INVALID_INPUT")})

    src = await run_in_threadpool(get_source_by_delimiter, source)
    if src is None or not src.get("enabled"):
        return JSONResponse(status_code=404, content={"error": "출처를 찾을 수 없습니다."})

    # 규정(원격 PDF 목록): 제목으로 공개 URL을 찾아 허용 호스트면 브라우저를 그쪽으로 보낸다.
    if src.get("kind") == "pdf_index":
        items = await run_in_threadpool(parse_index, Path(src["root_path"]))
        url = next((it["url"] for it in items if it.get("title") == path), None)
        if not url:
            return JSONResponse(status_code=404, content={"error": "문서를 찾을 수 없습니다."})
        host = (urlparse(url).hostname or "").lower()
        if not url.lower().startswith("https://") or host not in PDF_FETCH_ALLOWLIST:
            return JSONResponse(status_code=400, content={"error": "허용되지 않은 링크입니다."})
        return RedirectResponse(url)

    # 로컬 파일: 화이트리스트 관문으로만 해석(root 밖/트래버설/심볼릭링크/확장자 위반 차단).
    root = Path(src["root_path"])
    suffixes = parse_suffix_set(src["suffixes"])

    def _resolve():
        allowed = build_allowed_files(root, suffixes)
        return resolve_safe(path, allowed, root, suffixes)

    try:
        safe = await run_in_threadpool(_resolve)
    except PermissionError:
        return JSONResponse(status_code=403, content={"error": error_message("PermissionError")})
    except Exception:
        return JSONResponse(status_code=404, content={"error": "문서를 찾을 수 없습니다."})

    # attachment + octet-stream 으로 브라우저가 인라인 렌더 대신 다운로드하게 한다.
    return FileResponse(str(safe), filename=safe.name, media_type="application/octet-stream")
