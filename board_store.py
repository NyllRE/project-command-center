"""Server side board state for Project Command Center: three tables in one local SQLite file.

The board used to live entirely in the Desktop half's ``ctx.storage``, so an agent could not see
or move a project's status and a status did not survive a reinstall of the Desktop half. This
module is the single writer of that state:

  * ``status``  one row per project: backlog / active / review / blocked / done, a note, who wrote
    it (``user``, ``agent``, ``auto``) and when.
  * ``link``    one row per session: the project it works in, plus the issue or PR the session is
    attached to. The Desktop reads it for the running indicator; the tools write it.
  * ``project`` the ids, labels and absolute paths the board knows about, upserted by the Desktop
    half (``POST /board/projects``) so path based resolution has something to match.

Storage is ``plugins.plugin_storage.plugin_db`` (public, see ``website/docs/developer-guide/plugins/
public-api.md``): ``<hermes home>/plugin-data/project-command-center/board.db``, WAL, profile scoped,
``check_same_thread=False`` and the caller owns the transaction. Every call here opens its own
connection, keeps no rows in memory and writes inside one short ``BEGIN IMMEDIATE`` transaction, so
the gateway process and the agent process can write the same file at the same time.

No network, no shell, no credentials: this module reads only local SQLite files (the board, and
``state.db`` when a runtime session id has to be mapped back to its compression lineage) through
``plugins.plugin_storage`` and ``agent.runtime_cwd``.

Write failures are values (``{"ok": False, "code": ...}``), so a route or a tool can always answer.
The one exception is the database itself: if the file cannot be opened or read, the ``sqlite3``
error propagates, because there is no row to describe. Callers (``dashboard/plugin_api.py`` and the
tools' guard) turn that into ``code: "store_error"``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from agent.runtime_cwd import resolve_agent_cwd
from plugins.plugin_storage import plugin_data_dir, plugin_db

log = logging.getLogger(__name__)

PLUGIN_NAME = "project-command-center"
DB_FILENAME = "board.db"

STATUSES = ("backlog", "active", "review", "blocked", "done")
SOURCES = ("user", "agent", "auto")
KINDS = ("issue", "pr")
# How a link row's project was decided: the session's own folder, the Desktop, or an agent tool.
# Only ``cwd`` may auto-link a PR from a repository the row does not name.
RESOLVED_BY = ("cwd", "desktop", "agent")

_SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS status (
    project_id TEXT PRIMARY KEY,
    status TEXT CHECK (status IN ('backlog', 'active', 'review', 'blocked', 'done')),
    note TEXT,
    source TEXT CHECK (source IN ('user', 'agent', 'auto')),
    updated_at REAL
)
""",
    """
CREATE TABLE IF NOT EXISTS link (
    session_id TEXT PRIMARY KEY,
    project_id TEXT,
    repo TEXT,
    issue INTEGER,
    pr INTEGER,
    pr_url TEXT,
    title TEXT,
    resolved_by TEXT,
    created_at REAL,
    updated_at REAL
)
""",
    """
CREATE TABLE IF NOT EXISTS project (
    project_id TEXT PRIMARY KEY,
    label TEXT,
    paths TEXT,
    updated_at REAL
)
""",
    "CREATE INDEX IF NOT EXISTS status_updated_idx ON status (updated_at)",
    "CREATE INDEX IF NOT EXISTS link_project_idx ON link (project_id)",
)

# Columns added after a database was first created. ``_ensure_schema`` applies them once per file.
_MIGRATIONS = (
    ("link", "resolved_by", "TEXT"),
)

_LINK_COLUMNS = ("session_id, project_id, repo, issue, pr, pr_url, title, resolved_by, "
                 "created_at, updated_at")

# The schema is created once per process per database path. This is import bookkeeping, not board
# state: no row is ever held here, every read goes to the file.
_schema_lock = threading.Lock()
_schema_ready: set[str] = set()

# Read-only SessionDB handle for the compression lineage lookup (see ``_read_state_db``).
_state_lock = threading.Lock()
_STATE_DB: Any = None
_STATE_KEY = ""


# --- Plumbing ----------------------------------------------------------------


def _db_path() -> str:
    return str((plugin_data_dir(PLUGIN_NAME) / DB_FILENAME).resolve())


def _connect() -> sqlite3.Connection:
    """An autocommit connection (explicit BEGIN), schema created on first use in this process."""
    conn = plugin_db(PLUGIN_NAME, DB_FILENAME)
    conn.isolation_level = None  # explicit BEGIN IMMEDIATE: no implicit transaction upgrades
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the tables once per process per database path, and migrate older files.

    The cache is bookkeeping, not proof: a ``board.db`` deleted or replaced under a running process
    leaves the cache saying "ready" while every query answers ``no such table``. A cache hit is
    therefore verified against ``sqlite_master``, and any ``DatabaseError`` clears the entry so the
    next call rebuilds instead of failing until a restart.
    """
    key = _db_path()
    if key in _schema_ready and _tables_present(conn):
        return
    with _schema_lock:
        if key in _schema_ready and _tables_present(conn):
            return
        _schema_ready.discard(key)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _SCHEMA:
                conn.execute(statement)
            for table, column, declaration in _MIGRATIONS:
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        except BaseException:
            _rollback(conn)
            _schema_ready.discard(key)
            raise
        conn.execute("COMMIT")
        _schema_ready.add(key)


def _tables_present(conn: sqlite3.Connection) -> bool:
    """True when the three board tables exist. One cheap ``sqlite_master`` read per call."""
    try:
        row = conn.execute(
            "SELECT (SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'status')"
            " + (SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'link')"
            " + (SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'project')"
        ).fetchone()
    except sqlite3.DatabaseError:
        return False
    return bool(row and row[0] == 3)


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:  # already rolled back by SQLite (a fatal constraint), nothing to undo
        pass


@contextmanager
def _session() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    except sqlite3.DatabaseError:
        # The file moved, was replaced or is unreadable: drop the cache so the next call rebuilds once.
        _schema_ready.discard(_db_path())
        raise
    finally:
        conn.close()


@contextmanager
def _write(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One short write transaction. ``BEGIN IMMEDIATE`` takes the write lock up front, so two
    processes queue on ``busy_timeout`` (set by ``plugin_db``) instead of deadlocking on an upgrade."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        _rollback(conn)
        raise
    conn.execute("COMMIT")


def _fail(code: str, error: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": error, **extra}


def _ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, "code": "ok", **payload}


def _clean(value: Optional[str]) -> str:
    return str(value or "").strip()


def _status_row(row: Sequence[Any]) -> dict[str, Any]:
    project_id, status, note, source, updated_at = row
    return {"project_id": project_id, "status": status, "note": note or "",
            "source": source or "user", "updated_at": updated_at}


def _link_row(row: Sequence[Any]) -> dict[str, Any]:
    (session_id, project_id, repo, issue, pr, pr_url, title, resolved_by,
     created_at, updated_at) = row
    return {"session_id": session_id, "project_id": project_id or "", "repo": repo or "",
            "issue": issue, "pr": pr, "pr_url": pr_url, "title": title or "",
            "resolved_by": resolved_by or "", "created_at": created_at, "updated_at": updated_at}


def _paths(value: Any) -> list[str]:
    """A project's paths: a JSON array in the column, a list or JSON string from a caller."""
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else []
        except ValueError:
            value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    out: list[str] = []
    for item in value:
        text = _clean(item)
        if text and text not in out:
            out.append(text)
    return out


def _project_row(row: Sequence[Any]) -> dict[str, Any]:
    project_id, label, paths, updated_at = row
    return {"project_id": project_id, "label": label or project_id,
            "paths": _paths(paths), "updated_at": updated_at}


def _project_exists(conn: sqlite3.Connection, project_id: str) -> bool:
    return conn.execute("SELECT 1 FROM project WHERE project_id = ?", (project_id,)).fetchone() is not None


def _project_table_empty(conn: sqlite3.Connection) -> bool:
    """True while the Desktop half has never synced a project (a fresh board)."""
    return conn.execute("SELECT 1 FROM project LIMIT 1").fetchone() is None


def _ensure_project(conn: sqlite3.Connection, project_id: str, label: Optional[str] = None,
                    paths: Optional[Any] = None, *, force: bool = False) -> None:
    """Insert a bare project row when the store has never seen this id; never overwrite one."""
    now = time.time()
    conn.execute(
        "INSERT INTO project (project_id, label, paths, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(project_id) DO UPDATE SET label = excluded.label, paths = excluded.paths, "
        "updated_at = excluded.updated_at WHERE ? = 1",
        (project_id, label or project_id, json.dumps(_paths(paths)), now, 1 if force else 0),
    )


# --- Reads -------------------------------------------------------------------


def board() -> dict[str, Any]:
    """Every row the board renders: statuses, session links, known projects."""
    with _session() as conn:
        statuses = [_status_row(r) for r in conn.execute(
            "SELECT project_id, status, note, source, updated_at FROM status "
            "ORDER BY updated_at DESC, project_id")]
        links = [_link_row(r) for r in conn.execute(
            f"SELECT {_LINK_COLUMNS} FROM link ORDER BY updated_at DESC, session_id")]
        projects = [_project_row(r) for r in conn.execute(
            "SELECT project_id, label, paths, updated_at FROM project ORDER BY project_id")]
    return _ok(statuses=statuses, links=links, projects=projects, projects_synced=len(projects))


def get_status(project_id: Optional[str]) -> Optional[dict[str, Any]]:
    pid = _clean(project_id)
    if not pid:
        return None
    with _session() as conn:
        row = conn.execute(
            "SELECT project_id, status, note, source, updated_at FROM status WHERE project_id = ?",
            (pid,)).fetchone()
    return _status_row(row) if row else None


def get_link(session_id: Optional[str]) -> Optional[dict[str, Any]]:
    sid = _clean(session_id)
    if not sid:
        return None
    with _session() as conn:
        row = conn.execute(
            f"SELECT {_LINK_COLUMNS} FROM link WHERE session_id = ?", (sid,)).fetchone()
    return _link_row(row) if row else None


def get_project(project_id: Optional[str]) -> Optional[dict[str, Any]]:
    pid = _clean(project_id)
    if not pid:
        return None
    with _session() as conn:
        row = conn.execute(
            "SELECT project_id, label, paths, updated_at FROM project WHERE project_id = ?",
            (pid,)).fetchone()
    return _project_row(row) if row else None


def _read_state_db() -> Any:
    """One read-only ``SessionDB`` handle per ``state.db`` path, kept for the process.

    ``pre_llm_call`` fires every turn, so constructing a ``SessionDB`` per call costs more than the
    lookup itself (measured ~2 ms of a 3.2 ms miss). The handle is code plus a read pool, not row
    state, and the lineage question is a read: one handle, reused until the resolved path changes
    (a profile switch), is both correct and cheap.
    """
    global _STATE_DB, _STATE_KEY
    from hermes_constants import get_hermes_home

    path = (get_hermes_home() / "state.db").resolve()
    if not path.exists():
        return None
    with _state_lock:
        if _STATE_DB is None or _STATE_KEY != str(path):
            from hermes_state import SessionDB

            if _STATE_DB is not None:
                try:
                    _STATE_DB.close()
                except Exception:  # a closed or quarantined handle must not block the new one
                    pass
            _STATE_DB = SessionDB(path, read_only=True)
            _STATE_KEY = str(path)
        return _STATE_DB


def _compression_lineage(session_id: str) -> list[str]:
    """The compression ancestors of a runtime session id, root first; ``[]`` when unavailable.

    Context compression ends a session and continues it in a child id, so a hook or a tool is handed
    the child while the Desktop stored the link row under an ancestor. ``hermes_state`` is public API
    and ``get_compression_lineage`` is the same root walk a resume uses; anything unexpected here
    (no state.db, an old schema, a locked file) is "no ancestors", never a hook failure.
    """
    if not session_id:
        return []
    try:
        db = _read_state_db()
        if db is None:
            return []
        return [str(item) for item in (db.get_compression_lineage(session_id) or []) if item]
    except Exception:
        log.debug("%s: compression lineage unavailable for %s", PLUGIN_NAME, session_id,
                  exc_info=True)
    return []


def _link_on(conn: sqlite3.Connection, session_id: str) -> Optional[Sequence[Any]]:
    return conn.execute(
        f"SELECT {_LINK_COLUMNS} FROM link WHERE session_id = ?", (session_id,)).fetchone()


def _resolve_link_on(conn: sqlite3.Connection, session_id: str) -> tuple[str, Optional[Sequence[Any]]]:
    """``(key, row)`` for a runtime session id: itself when it owns a row, else its lineage ancestor.

    The key returned is the id the row is stored under, so a caller that writes back (attach a PR,
    move a lane) updates the row the Desktop reads instead of creating an orphan under the new id.
    """
    row = _link_on(conn, session_id)
    if row is not None:
        return session_id, row
    for ancestor in _compression_lineage(session_id):
        if ancestor == session_id:
            continue
        row = _link_on(conn, ancestor)
        if row is not None:
            return ancestor, row
    return session_id, None


def resolve_link(session_id: Optional[str]) -> tuple[str, Optional[dict[str, Any]]]:
    """The link row a runtime session id owns, plus the stored key it lives under (see above)."""
    sid = _clean(session_id)
    if not sid:
        return "", None
    with _session() as conn:
        key, row = _resolve_link_on(conn, sid)
    return key, (_link_row(row) if row is not None else None)


def session_card(session_id: Optional[str]) -> dict[str, Any]:
    """Everything ``pre_llm_call`` needs, in one connection and three primary-key lookups.

    Bounded on purpose: the hook fires every turn, so it must not read every status, link and
    project row the board has ever held. A runtime id whose row lives under an older compression
    ancestor resolves through ``_resolve_link_on``.
    """
    sid = _clean(session_id)
    if not sid:
        return _ok(session_id="", link=None, status=None, project=None)
    with _session() as conn:
        key, row = _resolve_link_on(conn, sid)
        if row is None:
            return _ok(session_id=sid, link=None, status=None, project=None)
        link = _link_row(row)
        pid = _clean(link.get("project_id"))
        status_row = project_row = None
        if pid:
            status_row = conn.execute(
                "SELECT project_id, status, note, source, updated_at FROM status "
                "WHERE project_id = ?", (pid,)).fetchone()
            project_row = conn.execute(
                "SELECT project_id, label, paths, updated_at FROM project WHERE project_id = ?",
                (pid,)).fetchone()
    return _ok(session_id=key, link=link,
               status=_status_row(status_row) if status_row else None,
               project=_project_row(project_row) if project_row else None)


def card_for_project(project_id: Optional[str], session_id: Optional[str] = None) -> dict[str, Any]:
    """The same card shape as :func:`session_card` for a project resolved some other way.

    A session that was just opened from the board has no ``link`` row yet, so the turn-one
    context comes from its cwd: two primary-key lookups, no scan, ``link`` stays ``None``.
    """
    pid = _clean(project_id)
    if not pid:
        return _ok(session_id=session_id or "", link=None, status=None, project=None)
    with _session() as conn:
        status_row = conn.execute(
            "SELECT project_id, status, note, source, updated_at FROM status "
            "WHERE project_id = ?", (pid,)).fetchone()
        project_row = conn.execute(
            "SELECT project_id, label, paths, updated_at FROM project WHERE project_id = ?",
            (pid,)).fetchone()
    return _ok(session_id=session_id or "", link=None,
               status=_status_row(status_row) if status_row else None,
               project=_project_row(project_row) if project_row else None)


def projects() -> list[dict[str, Any]]:
    with _session() as conn:
        return [_project_row(r) for r in conn.execute(
            "SELECT project_id, label, paths, updated_at FROM project ORDER BY project_id")]


def project_exists(project_id: Optional[str]) -> bool:
    pid = _clean(project_id)
    if not pid:
        return False
    with _session() as conn:
        return _project_exists(conn, pid)


# --- Writes ------------------------------------------------------------------


def set_status(project_id: Optional[str], status: Optional[str], note: Optional[str] = None,
               source: str = "user", *, auto_project: bool = False) -> dict[str, Any]:
    """Write one project's status. A note passed here replaces the row's note; omitting it (or
    passing an empty string) keeps whatever note is already on the card.

    ``auto_project`` lets the agent paths create a bare project row, but only on a board whose
    project table is still empty — a fresh install where the Desktop has not synced yet. Once the
    Desktop has synced anything, an id the table does not know is a typo, and both the tools and
    the routes answer ``unknown_project`` instead of growing a ghost project.
    """
    pid = _clean(project_id)
    if not pid:
        return _fail("empty_project", "project_id is required")
    state = _clean(status).lower()
    if state not in STATUSES:
        return _fail("bad_status", f"status must be one of {', '.join(STATUSES)}", status=status)
    src = _clean(source).lower() or "user"
    if src not in SOURCES:
        return _fail("bad_source", f"source must be one of {', '.join(SOURCES)}")
    now = time.time()
    with _session() as conn:
        with _write(conn):
            if not _project_exists(conn, pid):
                if not (auto_project and _project_table_empty(conn)):
                    return _fail("unknown_project", f"{pid!r} is not a synced project", project_id=pid)
                _ensure_project(conn, pid)
            conn.execute(
                "INSERT INTO status (project_id, status, note, source, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(project_id) DO UPDATE SET status = excluded.status, "
                "note = COALESCE(excluded.note, status.note), "
                "source = excluded.source, updated_at = excluded.updated_at",
                (pid, state, _clean(note) or None, src, now))
            row = conn.execute(
                "SELECT project_id, status, note, source, updated_at FROM status WHERE project_id = ?",
                (pid,)).fetchone()
            return _ok(status=_status_row(row))


def move_status_if_current(project_id: Optional[str], expect: Optional[str], new: Optional[str],
                           note: Optional[str] = None, source: str = "auto") -> dict[str, Any]:
    """Compare-and-set a lane inside one ``BEGIN IMMEDIATE`` transaction.

    The read of the current lane and the update that depends on it share the write lock, so a user's
    move to ``done`` landing between a caller's earlier read and this write cannot be overwritten:
    the CAS answers ``code: "status_changed"`` with the lane that won, and the caller leaves it.
    """
    pid = _clean(project_id)
    want = _clean(expect).lower()
    state = _clean(new).lower()
    if not pid:
        return _fail("empty_project", "project_id is required")
    if state not in STATUSES:
        return _fail("bad_status", f"status must be one of {', '.join(STATUSES)}", status=new)
    src = _clean(source).lower() or "auto"
    if src not in SOURCES:
        return _fail("bad_source", f"source must be one of {', '.join(SOURCES)}")
    now = time.time()
    with _session() as conn:
        with _write(conn):
            row = conn.execute(
                "SELECT status FROM status WHERE project_id = ?", (pid,)).fetchone()
            if row is None:
                return _fail("status_missing", f"{pid!r} has no status row", project_id=pid)
            current = _clean(row[0]).lower()
            if current != want:
                return _fail("status_changed", f"{pid!r} is {current!r}, not {want!r}",
                             project_id=pid, current=current)
            conn.execute(
                "UPDATE status SET status = ?, note = COALESCE(?, note), source = ?, updated_at = ? "
                "WHERE project_id = ? AND status = ?", (state, _clean(note) or None, src, now, pid, want))
            fresh = conn.execute(
                "SELECT project_id, status, note, source, updated_at FROM status WHERE project_id = ?",
                (pid,)).fetchone()
            return _ok(status=_status_row(fresh), changed=True)


def set_projects(items: Any) -> dict[str, Any]:
    """Idempotent upsert of the project table: ``[{id, label, paths}]``. Rows not listed are kept,
    because a status or a link may still point at one.

    ``projects_synced`` counts the rows processed; ``changed`` counts only those whose label or
    paths actually differ, which is what the route uses to decide whether to broadcast."""
    if not isinstance(items, (list, tuple)):
        return _fail("bad_projects", "projects must be a list of {id, label, paths}")
    now = time.time()
    written = changed = 0
    with _session() as conn:
        with _write(conn):
            for item in items:
                if not isinstance(item, dict):
                    continue
                pid = _clean(item.get("id") or item.get("project_id"))
                if not pid:
                    continue
                label = _clean(item.get("label")) or pid
                paths = _paths(item.get("paths"))
                before = conn.execute(
                    "SELECT label, paths FROM project WHERE project_id = ?", (pid,)).fetchone()
                _ensure_project(conn, pid, label, paths, force=True)
                written += 1
                if (before is None or _clean(before[0]) != label
                        or _paths(before[1]) != paths):
                    changed += 1
    return _ok(projects_synced=written, changed=changed)


def seed(statuses: Any) -> dict[str, Any]:
    """Restore statuses on a board that has none. Writes only while the ``status`` table is empty,
    so a second call can never overwrite live state."""
    if not isinstance(statuses, (list, tuple)) or not statuses:
        return _fail("empty_seed", "statuses must be a non empty list")
    now = time.time()
    rows: list[tuple[str, str, Optional[str], str]] = []
    skipped = 0
    for item in statuses:
        if not isinstance(item, dict):
            skipped += 1
            continue
        pid = _clean(item.get("project_id") or item.get("id"))
        state = _clean(item.get("status")).lower()
        src = _clean(item.get("source")).lower() or "user"
        if not pid or state not in STATUSES:
            skipped += 1
            continue
        rows.append((pid, state, _clean(item.get("note")) or None, src if src in SOURCES else "user"))
    if not rows:
        return _fail("empty_seed", "no status row had a project id and a valid status", skipped=skipped)
    with _session() as conn:
        with _write(conn):
            occupied = conn.execute("SELECT 1 FROM status LIMIT 1").fetchone() is not None
            if occupied:
                return _fail("seed_not_empty", "the board already has statuses; seed is refused")
            for pid, state, note, src in rows:
                if not _project_exists(conn, pid):
                    _ensure_project(conn, pid)
                conn.execute(
                    "INSERT INTO status (project_id, status, note, source, updated_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(project_id) DO UPDATE SET status = excluded.status, note = excluded.note, "
                    "source = excluded.source, updated_at = excluded.updated_at",
                    (pid, state, note, src, now))
    return _ok(seeded=len(rows), skipped=skipped)


def set_link(session_id: Optional[str], project_id: Optional[str], repo: Optional[str] = None,
             issue: Any = None, pr: Any = None, pr_url: Optional[str] = None,
             title: Optional[str] = None, *, auto_project: bool = False,
             resolved_by: Optional[str] = None) -> dict[str, Any]:
    """Attach a session to a project, and to an issue and/or a PR. One row per session: a second
    call fills the columns it was given and leaves the rest alone.

    ``resolved_by`` records how the project was chosen (``cwd`` / ``desktop`` / ``agent``); the
    auto-link hook uses it to decide whether a repo-less row may claim a PR from any repository.
    """
    sid = _clean(session_id)
    pid = _clean(project_id)
    if not sid:
        return _fail("empty_session", "session_id is required")
    if not pid:
        return _fail("empty_project", "project_id is required")
    by = _clean(resolved_by).lower() or None
    if by is not None and by not in RESOLVED_BY:
        return _fail("bad_resolved_by", f"resolved_by must be one of {', '.join(RESOLVED_BY)}")
    numbers: dict[str, Optional[int]] = {}
    for name, value in (("issue", issue), ("pr", pr)):
        if value in (None, ""):
            numbers[name] = None
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            return _fail(f"bad_{name}", f"{name} must be a positive integer")
        if number <= 0:
            return _fail(f"bad_{name}", f"{name} must be a positive integer")
        numbers[name] = number
    now = time.time()
    with _session() as conn:
        with _write(conn):
            if not _project_exists(conn, pid):
                if not (auto_project and _project_table_empty(conn)):
                    return _fail("unknown_project", f"{pid!r} is not a synced project", project_id=pid)
                _ensure_project(conn, pid)
            conn.execute(
                "INSERT INTO link (session_id, project_id, repo, issue, pr, pr_url, title, "
                "resolved_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET project_id = excluded.project_id, "
                "repo = COALESCE(NULLIF(excluded.repo, ''), link.repo), "
                "issue = COALESCE(excluded.issue, link.issue), pr = COALESCE(excluded.pr, link.pr), "
                "pr_url = COALESCE(excluded.pr_url, link.pr_url), "
                "title = COALESCE(NULLIF(excluded.title, ''), link.title), "
                "resolved_by = COALESCE(NULLIF(excluded.resolved_by, ''), link.resolved_by), "
                "updated_at = excluded.updated_at",
                (sid, pid, _clean(repo), numbers["issue"], numbers["pr"], _clean(pr_url) or None,
                 _clean(title) or None, by, now, now))
            row = conn.execute(
                f"SELECT {_LINK_COLUMNS} FROM link WHERE session_id = ?", (sid,)).fetchone()
            return _ok(link=_link_row(row))


def detach_item(session_id: Optional[str], kind: str, number: Any) -> dict[str, Any]:
    """Clear one issue or PR off a session's link row. Returns ``not_attached`` when the row does not
    hold that number, so a tool can answer honestly instead of pretending."""
    sid = _clean(session_id)
    which = _clean(kind).lower()
    if not sid:
        return _fail("empty_session", "session_id is required")
    if which not in KINDS:
        return _fail("bad_kind", f"kind must be one of {', '.join(KINDS)}")
    try:
        want = int(number)
    except (TypeError, ValueError):
        return _fail("bad_number", "number must be a positive integer")
    column = "issue" if which == "issue" else "pr"
    now = time.time()
    with _session() as conn:
        with _write(conn):
            row = conn.execute(
                f"SELECT {_LINK_COLUMNS} FROM link WHERE session_id = ?", (sid,)).fetchone()
            if row is None:
                return _fail("not_attached", "this session has no link row", session_id=sid)
            row = _link_row(row)
            if row[column] != want:
                return _fail("not_attached", f"session is not attached to {which} #{want}",
                             session_id=sid, attached=row[column])
            conn.execute(
                f"UPDATE link SET {column} = NULL, updated_at = ? WHERE session_id = ?", (now, sid))
            if column == "issue" and not row.get("pr"):
                conn.execute("UPDATE link SET pr_url = NULL WHERE session_id = ?", (sid,))
            fresh = conn.execute(
                f"SELECT {_LINK_COLUMNS} FROM link WHERE session_id = ?", (sid,)).fetchone()
            return _ok(link=_link_row(fresh))


# --- Resolution --------------------------------------------------------------


def _label_match(conn: sqlite3.Connection, wanted: str) -> Optional[str]:
    """The one project whose label matches (case insensitive); None when absent or ambiguous."""
    needle = wanted.casefold()
    hits = [row[0] for row in conn.execute("SELECT project_id, label FROM project")
            if (row[1] or row[0] or "").strip().casefold() == needle]
    return hits[0] if len(hits) == 1 else None


def _path_match(projects_with_paths: Sequence[tuple[str, list[str]]],
                cwd: Path) -> Optional[str]:
    """The project whose path is the longest prefix of *cwd* (the page you are in wins over its root)."""
    best: Optional[str] = None
    best_len = -1
    for pid, raw_paths in projects_with_paths:
        for raw in raw_paths:
            try:
                candidate = Path(raw).expanduser().resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            if cwd != candidate and candidate not in cwd.parents:
                continue
            size = len(candidate.parts)
            if size > best_len:
                best, best_len = pid, size
    return best


def resolve_project(session_id: Optional[str] = None, explicit: Optional[str] = None,
                    *, use_cwd: bool = True) -> Optional[str]:
    """Which project this is about (see ``resolve_project_detail``), or ``None``."""
    return resolve_project_detail(session_id=session_id, explicit=explicit, use_cwd=use_cwd)[0]


def resolve_project_detail(session_id: Optional[str] = None, explicit: Optional[str] = None,
                           *, use_cwd: bool = True) -> tuple[Optional[str], Optional[str]]:
    """Which project this is about, and how that was decided.

    1. ``explicit`` — the id a tool was handed, or the unique label that names it. An id the project
    table has never seen is returned as given: the caller (a tool) may be naming a project before
    the Desktop synced it, and the write path is what validates existence.
    2. a unique project ``label`` match on ``explicit``.
    3. the ``link`` row for ``session_id`` (following the compression lineage when the runtime id is
    newer than the stored one).
    4. the longest path prefix of ``agent.runtime_cwd.resolve_agent_cwd()`` over ``project.paths``.
    5. ``(None, None)``.

    The second value is ``explicit`` / ``label`` / ``session`` / ``cwd`` or ``None``; the tools turn
    ``cwd`` into a link row's ``resolved_by = "cwd"`` so the PR auto-link rule can trust it.
    """
    explicit_id = _clean(explicit)
    sid = _clean(session_id)
    with _session() as conn:
        if explicit_id:
            if _project_exists(conn, explicit_id):
                return explicit_id, "explicit"
            by_label = _label_match(conn, explicit_id)
            if by_label:
                return by_label, "label"
            return explicit_id, "explicit"
        if sid:
            _, row = _resolve_link_on(conn, sid)
            if row is not None:
                pid = _clean(_link_row(row).get("project_id"))
                if pid:
                    return pid, "session"
        if not use_cwd:
            return None, None
        rows = [(row[0], _paths(row[1])) for row in conn.execute("SELECT project_id, paths FROM project")]
    if not rows:
        return None, None
    try:
        cwd = Path(resolve_agent_cwd()).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None, None
    matched = _path_match(rows, cwd)
    return matched, ("cwd" if matched else None)
