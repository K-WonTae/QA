# app/database.py
"""
SQLite 접근. 모든 쿼리는 파라미터 바인딩만 사용한다.
f-string / % / .format() 으로 SQL을 조립하는 코드는 한 줄도 두지 않는다.
"""
import sqlite3
from contextlib import contextmanager

from app.config import DB_PATH, DATA_DIR
from app.logging_conf import _lock_down


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _conn() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "  session_id TEXT PRIMARY KEY,"
            "  created_at TEXT NOT NULL"
            ")"
        )
        _ensure_column(cur, "sessions", "last_source_delimiter", "TEXT")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS chat_messages ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  session_id TEXT NOT NULL,"
            "  role TEXT NOT NULL,"
            "  content TEXT NOT NULL,"
            "  selected_files TEXT,"
            "  created_at TEXT NOT NULL"
            ")"
        )
        # A-5: 실토큰 usage 계측 컬럼(기존 DB 마이그레이션 포함). assistant 행에만 채워진다.
        _ensure_column(cur, "chat_messages", "input_tokens", "INTEGER")
        _ensure_column(cur, "chat_messages", "output_tokens", "INTEGER")
        _ensure_column(cur, "chat_messages", "cache_read", "INTEGER")
        _ensure_column(cur, "chat_messages", "cache_write", "INTEGER")
        _ensure_column(cur, "chat_messages", "model", "TEXT")
        # 질의 대상 폴더(출처) 등록부. delimiter는 유일.
        cur.execute(
            "CREATE TABLE IF NOT EXISTS sources ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  delimiter TEXT NOT NULL UNIQUE,"
            "  label TEXT NOT NULL,"
            "  root_path TEXT NOT NULL,"
            "  suffixes TEXT NOT NULL,"
            "  division TEXT NOT NULL DEFAULT '학사',"
            "  kind TEXT NOT NULL DEFAULT 'local',"
            "  is_default INTEGER NOT NULL DEFAULT 0,"
            "  enabled INTEGER NOT NULL DEFAULT 1,"
            "  created_at TEXT NOT NULL"
            ")"
        )
        # 기존 DB 마이그레이션: kind 컬럼 보강
        _ensure_column(cur, "sources", "kind", "TEXT NOT NULL DEFAULT 'local'")
        _ensure_column(cur, "sources", "division", "TEXT NOT NULL DEFAULT '학사'")
        # 출처 간 고정 연계: 주 출처(source_id)로 질의하면 linked_id 출처도 함께 본다.
        # 방향성 있음(A→B 가 B→A 를 뜻하지 않음). 한 쌍은 한 번만.
        cur.execute(
            "CREATE TABLE IF NOT EXISTS source_links ("
            "  source_id INTEGER NOT NULL,"
            "  linked_id INTEGER NOT NULL,"
            "  PRIMARY KEY (source_id, linked_id)"
            ")"
        )
        # 앱 설정 메타(예: 기본 출처 최초 시드 여부)
        cur.execute(
            "CREATE TABLE IF NOT EXISTS app_meta ("
            "  key TEXT PRIMARY KEY,"
            "  value TEXT"
            ")"
        )
        # 답변 피드백(F-3). message_id 당 1건(재평가 시 upsert). comment는 저장 전용.
        cur.execute(
            "CREATE TABLE IF NOT EXISTS feedback ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  message_id INTEGER NOT NULL UNIQUE,"
            "  rating TEXT NOT NULL,"
            "  comment TEXT,"
            "  created_at TEXT NOT NULL"
            ")"
        )
    # DB 파일 권한 제한 (11장)
    _lock_down(DB_PATH)


def get_meta(key: str) -> str | None:
    with _conn() as cur:
        cur.execute("SELECT value FROM app_meta WHERE key = ?", (key,))
        row = cur.fetchone()
    return row[0] if row else None


def set_meta(key: str, value: str) -> None:
    with _conn() as cur:
        cur.execute(
            "INSERT INTO app_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ---- 출처(폴더) 관리 ----

_SOURCE_COLS = (
    "id", "delimiter", "label", "root_path", "suffixes",
    "division", "kind", "is_default", "enabled", "created_at",
)

# SELECT 절에서 공통으로 쓰는 컬럼 목록(순서는 _SOURCE_COLS와 일치).
_SOURCE_SELECT = (
    "id, delimiter, label, root_path, suffixes, division, kind, "
    "is_default, enabled, created_at"
)


def _row_to_source(row: tuple) -> dict:
    return dict(zip(_SOURCE_COLS, row))


def _ensure_column(cur, table: str, column: str, definition: str) -> None:
    cur.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in cur.fetchall()}
    if column not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def seed_default_source(delimiter: str, label: str, root_path: str,
                        suffixes: str, created_at: str,
                        kind: str = "local", division: str = "학사") -> None:
    """
    내장 출처(@지식·@규정)를 '최초 1회만' 등록한다(INSERT OR IGNORE).
    이후에는 사용자가 자유롭게 수정/삭제할 수 있으므로 강제로 덮어쓰지 않는다.
    (호출부에서 app_meta 플래그로 최초 실행에만 호출한다.)
    """
    with _conn() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO sources "
            "(delimiter, label, root_path, suffixes, division, kind, is_default, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?)",
            (delimiter, label, root_path, suffixes, division, kind, created_at),
        )


def list_sources(enabled_only: bool = False) -> list[dict]:
    with _conn() as cur:
        if enabled_only:
            cur.execute(
                "SELECT " + _SOURCE_SELECT + " FROM sources WHERE enabled = 1 "
                "ORDER BY division ASC, is_default DESC, id ASC"
            )
        else:
            cur.execute(
                "SELECT " + _SOURCE_SELECT + " FROM sources "
                "ORDER BY division ASC, is_default DESC, id ASC"
            )
        rows = cur.fetchall()
    return [_row_to_source(r) for r in rows]


def get_source_by_delimiter(delimiter: str) -> dict | None:
    with _conn() as cur:
        cur.execute(
            "SELECT " + _SOURCE_SELECT + " FROM sources WHERE delimiter = ?",
            (delimiter,),
        )
        row = cur.fetchone()
    return _row_to_source(row) if row else None


def get_source_by_id(source_id: int) -> dict | None:
    with _conn() as cur:
        cur.execute(
            "SELECT " + _SOURCE_SELECT + " FROM sources WHERE id = ?",
            (source_id,),
        )
        row = cur.fetchone()
    return _row_to_source(row) if row else None


def count_sources() -> int:
    with _conn() as cur:
        cur.execute("SELECT COUNT(*) FROM sources")
        return int(cur.fetchone()[0])


def add_source(delimiter: str, label: str, root_path: str, suffixes: str,
               division: str,
               created_at: str) -> int:
    """사용자 등록 출처 추가. delimiter 중복 시 sqlite3.IntegrityError."""
    with _conn() as cur:
        cur.execute(
            "INSERT INTO sources "
            "(delimiter, label, root_path, suffixes, division, is_default, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, 0, 1, ?)",
            (delimiter, label, root_path, suffixes, division, created_at),
        )
        return int(cur.lastrowid)


def delete_source(source_id: int) -> bool:
    """기본 출처를 포함해 어떤 출처든 삭제한다(사용자 요청).
    이 출처가 얽힌 연계 관계(양방향 모두)도 함께 정리한다."""
    with _conn() as cur:
        cur.execute(
            "DELETE FROM source_links WHERE source_id = ? OR linked_id = ?",
            (source_id, source_id),
        )
        cur.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        return cur.rowcount > 0


# ---- 출처 간 고정 연계 ----

def get_link_ids(source_id: int) -> list[int]:
    """주 출처에 연계 등록된 출처 id 목록(존재 여부 무관, 등록 순)."""
    with _conn() as cur:
        cur.execute(
            "SELECT linked_id FROM source_links WHERE source_id = ? ORDER BY linked_id ASC",
            (source_id,),
        )
        return [int(r[0]) for r in cur.fetchall()]


def get_all_link_ids() -> dict[int, list[int]]:
    """{주 출처 id: [연계 id, ...]} 전체 맵(목록 화면에서 한 번에 그릴 때 사용)."""
    with _conn() as cur:
        cur.execute("SELECT source_id, linked_id FROM source_links")
        rows = cur.fetchall()
    out: dict[int, list[int]] = {}
    for sid, lid in rows:
        out.setdefault(int(sid), []).append(int(lid))
    for v in out.values():
        v.sort()
    return out


def set_links(source_id: int, linked_ids: list[int]) -> None:
    """주 출처의 연계 목록을 통째로 교체한다(자기 자신·중복은 무시)."""
    with _conn() as cur:
        cur.execute("DELETE FROM source_links WHERE source_id = ?", (source_id,))
        seen: set[int] = set()
        for lid in linked_ids:
            if not isinstance(lid, int) or lid == source_id or lid in seen:
                continue
            seen.add(lid)
            cur.execute(
                "INSERT OR IGNORE INTO source_links (source_id, linked_id) VALUES (?, ?)",
                (source_id, lid),
            )


def get_linked_sources(source_id: int) -> list[dict]:
    """주 출처에 연계된 '활성' 출처들의 전체 레코드(자기 자신 제외, 등록 순)."""
    with _conn() as cur:
        cur.execute(
            "SELECT s.id, s.delimiter, s.label, s.root_path, s.suffixes, "
            "s.division, s.kind, s.is_default, s.enabled, s.created_at "
            "FROM source_links l JOIN sources s ON s.id = l.linked_id "
            "WHERE l.source_id = ? AND s.enabled = 1 AND s.id != ? "
            "ORDER BY s.id ASC",
            (source_id, source_id),
        )
        rows = cur.fetchall()
    return [_row_to_source(r) for r in rows]


def update_source(source_id: int, delimiter: str, label: str, root_path: str,
                  suffixes: str, division: str) -> bool:
    """출처의 사용자 편집 가능 필드를 수정한다(kind/is_default는 유지).
    delimiter 중복 시 sqlite3.IntegrityError."""
    with _conn() as cur:
        cur.execute(
            "UPDATE sources SET delimiter = ?, label = ?, root_path = ?, "
            "suffixes = ?, division = ? WHERE id = ?",
            (delimiter, label, root_path, suffixes, division, source_id),
        )
        return cur.rowcount > 0


@contextmanager
def _conn():
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    finally:
        conn.close()


def ensure_session(session_id: str, created_at: str) -> None:
    with _conn() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO sessions (session_id, created_at) VALUES (?, ?)",
            (session_id, created_at),
        )


def get_session_source(session_id: str) -> str | None:
    with _conn() as cur:
        cur.execute(
            "SELECT last_source_delimiter FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def set_session_source(session_id: str, delimiter: str) -> None:
    with _conn() as cur:
        cur.execute(
            "UPDATE sessions SET last_source_delimiter = ? WHERE session_id = ?",
            (delimiter, session_id),
        )


def save_message(session_id: str, role: str, content: str,
                 selected_files: str, created_at: str,
                 usage: dict | None = None) -> int:
    # ✅ 파라미터 바인딩만 사용. 저장한 행 id를 반환(S-0: 피드백/출처 연동용).
    u = usage or {}
    with _conn() as cur:
        cur.execute(
            "INSERT INTO chat_messages "
            "(session_id, role, content, selected_files, created_at, "
            " input_tokens, output_tokens, cache_read, cache_write, model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, role, content, selected_files, created_at,
             u.get("input_tokens"), u.get("output_tokens"),
             u.get("cache_read"), u.get("cache_write"), u.get("model")),
        )
        return int(cur.lastrowid)


def get_recent_messages(session_id: str, limit: int = 4) -> list[tuple[str, str]]:
    """이전 대화 요약 구성을 위한 최근 메시지 (role, content)."""
    with _conn() as cur:
        cur.execute(
            "SELECT role, content FROM chat_messages "
            "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        rows = cur.fetchall()
    return list(reversed(rows))


# ---- 대화 이력 ----

def list_conversations(limit: int = 100) -> list[dict]:
    """
    메시지가 있는 세션을 최근 활동순으로 모은다.
    각 항목: {session_id, title(첫 사용자 질문), count, last_at}
    """
    with _conn() as cur:
        cur.execute(
            "SELECT session_id, COUNT(*) AS cnt, MAX(created_at) AS last_at, MAX(id) AS max_id "
            "FROM chat_messages GROUP BY session_id "
            "ORDER BY max_id DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
        convs = []
        for sid, cnt, last_at, _max_id in rows:
            cur.execute(
                "SELECT content FROM chat_messages "
                "WHERE session_id = ? AND role = 'user' ORDER BY id ASC LIMIT 1",
                (sid,),
            )
            r = cur.fetchone()
            title = (r[0].strip() if r and r[0] else "") or "(빈 대화)"
            if len(title) > 80:
                title = title[:80] + "…"
            convs.append({
                "session_id": sid, "count": cnt,
                "last_at": last_at, "title": title,
            })
    return convs


def get_conversation(session_id: str, limit: int = 500) -> list[dict]:
    """한 세션의 메시지를 시간순으로 반환."""
    with _conn() as cur:
        cur.execute(
            "SELECT role, content, selected_files, created_at FROM chat_messages "
            "WHERE session_id = ? ORDER BY id ASC LIMIT ?",
            (session_id, limit),
        )
        rows = cur.fetchall()
    return [
        {"role": r[0], "content": r[1],
         "files": (r[2].split(",") if r[2] else []), "created_at": r[3]}
        for r in rows
    ]


def delete_conversation(session_id: str) -> bool:
    """세션과 그 메시지를 모두 삭제."""
    with _conn() as cur:
        cur.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
        deleted = cur.rowcount
        cur.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
    return deleted > 0


# ---- 피드백(F-3) ----

def message_exists(message_id: int) -> bool:
    """주어진 id의 assistant 메시지가 실재하는지(임의 id 차단용)."""
    with _conn() as cur:
        cur.execute(
            "SELECT 1 FROM chat_messages WHERE id = ? AND role = 'assistant'",
            (message_id,),
        )
        return cur.fetchone() is not None


def save_feedback(message_id: int, rating: str, comment: str | None,
                  created_at: str) -> None:
    """답변 피드백 저장(파라미터 바인딩만). 같은 메시지 재평가 시 upsert."""
    with _conn() as cur:
        cur.execute(
            "INSERT INTO feedback (message_id, rating, comment, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(message_id) DO UPDATE SET "
            "  rating = excluded.rating, comment = excluded.comment, "
            "  created_at = excluded.created_at",
            (message_id, rating, comment, created_at),
        )


def list_negative_feedback(limit: int = 500) -> list[dict]:
    """👎 피드백을 답변·사용 출처·직전 질문과 함께 최근순으로 모은다(export/시드용)."""
    rows: list[dict] = []
    with _conn() as cur:
        cur.execute(
            "SELECT f.message_id, f.comment, f.created_at, "
            "       m.session_id, m.content, m.selected_files, m.id "
            "FROM feedback f JOIN chat_messages m ON m.id = f.message_id "
            "WHERE f.rating = 'down' "
            "ORDER BY f.created_at DESC LIMIT ?",
            (limit,),
        )
        fb = cur.fetchall()
        for mid, comment, created_at, session_id, answer, files, aid in fb:
            # 같은 세션에서 이 답변 직전의 사용자 질문을 찾는다.
            cur.execute(
                "SELECT content FROM chat_messages "
                "WHERE session_id = ? AND role = 'user' AND id < ? "
                "ORDER BY id DESC LIMIT 1",
                (session_id, aid),
            )
            qrow = cur.fetchone()
            rows.append({
                "message_id": mid,
                "question": (qrow[0] if qrow else ""),
                "answer": answer,
                "files": (files.split(",") if files else []),
                "comment": comment or "",
                "created_at": created_at,
            })
    return rows
