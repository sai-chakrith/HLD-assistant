"""Access control, SQLite schema, audit log and review decisions."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.config import settings

ROLES = {"viewer": 0, "reviewer": 1, "admin": 2}
DOC_TYPES = {"hld", "interface_spec", "architecture", "catalogue"}
SOURCE_STATUSES = {"approved", "synthetic"}
DECISIONS = {"approved", "rejected", "corrected"}
# item_type -> table holding that item (review_status column updated if present)
REVIEW_TABLES = {
    "component": "components", "port": "ports", "interface": "interfaces",
    "signal": "signals", "finding": "findings", "report": "reports", "answer": "answers",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(project_id TEXT PRIMARY KEY, name TEXT, created_at TEXT);
-- users.role is the DEFAULT role given to new memberships and the GLOBAL-admin flag (audit log access).
-- Authorisation inside a project uses project_members.role only.
CREATE TABLE IF NOT EXISTS users(
  username TEXT PRIMARY KEY,
  role TEXT NOT NULL CHECK(role IN ('viewer','reviewer','admin')));
CREATE TABLE IF NOT EXISTS project_members(
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  username TEXT NOT NULL REFERENCES users(username),
  role TEXT CHECK(role IN ('viewer','reviewer','admin')),
  PRIMARY KEY(project_id, username));
CREATE TABLE IF NOT EXISTS documents(
  doc_id TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES projects(project_id),
  title TEXT,
  doc_type TEXT NOT NULL CHECK(doc_type IN ('hld','interface_spec','architecture','catalogue')),
  version TEXT,
  source_status TEXT NOT NULL CHECK(source_status IN ('approved','synthetic')),
  sha256 TEXT NOT NULL, revisions_json TEXT, uploaded_by TEXT, uploaded_at TEXT,
  PRIMARY KEY(project_id, doc_id));
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT, action TEXT, target TEXT,
  details TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS reviews(
  id INTEGER PRIMARY KEY AUTOINCREMENT, item_type TEXT, item_id TEXT, decision TEXT,
  comment TEXT, reviewer TEXT, ts TEXT);
"""


class PermissionDenied(Exception):
    """Raised when a user lacks membership or role for an action."""


def get_conn(db_path: Path | str | None = None, same_thread: bool = True) -> sqlite3.Connection:
    """Open a connection (default: settings.db_path) with the schema ensured. ``same_thread=False`` for a
    connection that one request uses sequentially but FastAPI may open and close on different worker threads."""
    path = Path(db_path) if db_path else settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=same_thread)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Idempotent migrations. v2: project_members.role (copied from users.role for existing memberships)."""
    if not _has_column(conn, "project_members", "role"):
        conn.execute("ALTER TABLE project_members ADD COLUMN role TEXT")
    conn.execute("UPDATE project_members SET role=(SELECT role FROM users WHERE users.username="
                 "project_members.username) WHERE role IS NULL")
    conn.commit()


@contextmanager
def _use(conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
    """Yield the given connection, or a short-lived default one."""
    if conn is not None:
        yield conn
        return
    c = get_conn()
    try:
        yield c
    finally:
        c.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_project(project_id: str, name: str | None = None, conn: sqlite3.Connection | None = None) -> None:
    with _use(conn) as c:
        c.execute("INSERT OR IGNORE INTO projects VALUES(?,?,?)", (project_id, name or project_id, _now()))
        c.commit()


def add_user(username: str, role: str, conn: sqlite3.Connection | None = None) -> None:
    if role not in ROLES:
        raise ValueError(f"invalid role {role!r}")
    with _use(conn) as c:
        c.execute("INSERT OR REPLACE INTO users VALUES(?,?)", (username, role))
        c.commit()


def add_member(project_id: str, username: str, role: str | None = None,
               conn: sqlite3.Connection | None = None) -> None:
    """Add (or, when ``role`` is given, update) a membership. Default role = the user's global users.role."""
    if role is not None and role not in ROLES:
        raise ValueError(f"invalid role {role!r}")
    with _use(conn) as c:
        default = get_role(username, c)
        if default is None:
            raise ValueError(f"unknown user {username!r}")
        if role is None:
            c.execute("INSERT OR IGNORE INTO project_members(project_id,username,role) VALUES(?,?,?)",
                      (project_id, username, default))
        else:
            c.execute("INSERT INTO project_members(project_id,username,role) VALUES(?,?,?) "
                      "ON CONFLICT(project_id,username) DO UPDATE SET role=excluded.role",
                      (project_id, username, role))
        c.commit()


def get_member_role(user: str, project_id: str, conn: sqlite3.Connection | None = None) -> str | None:
    """Role of ``user`` in ``project_id`` (None if not a member)."""
    with _use(conn) as c:
        row = c.execute("SELECT role FROM project_members WHERE project_id=? AND username=?",
                        (project_id, user)).fetchone()
    return (row["role"] or get_role(user, c)) if row else None


def log_action(user: str | None, action: str, target: str = "", details: str | dict | None = None,
               conn: sqlite3.Connection | None = None) -> None:
    if isinstance(details, dict):
        details = json.dumps(details, default=str)
    with _use(conn) as c:
        c.execute("INSERT INTO audit_log(user,action,target,details,ts) VALUES(?,?,?,?,?)",
                  (user, action, target, details or "", _now()))
        c.commit()


def get_role(username: str, conn: sqlite3.Connection | None = None) -> str | None:
    with _use(conn) as c:
        row = c.execute("SELECT role FROM users WHERE username=?", (username,)).fetchone()
    return row["role"] if row else None


def is_member(user: str, project_id: str, conn: sqlite3.Connection | None = None) -> bool:
    with _use(conn) as c:
        return c.execute("SELECT 1 FROM project_members WHERE project_id=? AND username=?",
                         (project_id, user)).fetchone() is not None


def require_role(user: str, project_id: str, min_role: str, conn: sqlite3.Connection | None = None) -> str:
    """Check project membership AND the PER-PROJECT role (project_members.role) >= min_role.

    users.role is not consulted here (it is only the default for new memberships and the global-admin flag used by
    /audit), so a global admin who is not a member of a project is still refused: project isolation stays strict."""
    if min_role not in ROLES:
        raise ValueError(f"invalid role {min_role!r}")
    with _use(conn) as c:
        if get_role(user, c) is None:
            raise PermissionDenied(f"unknown user {user!r}")
        role = get_member_role(user, project_id, c)
        if role is None:
            raise PermissionDenied(f"{user!r} is not a member of project {project_id!r}")
        if ROLES[role] < ROLES[min_role]:
            raise PermissionDenied(f"{user!r} has role {role!r} in {project_id!r}, needs {min_role!r}")
    return role


def _has_column(c: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r["name"] == column for r in c.execute(f"PRAGMA table_info({table})"))


def review(item_type: str, item_id: str | int, decision: str, comment: str, user: str,
           project_id: str | None = None, conn: sqlite3.Connection | None = None) -> None:
    """Record a review decision. Only reviewer/admin; if project_id given, membership is required too."""
    if decision not in DECISIONS:
        raise ValueError(f"invalid decision {decision!r}")
    with _use(conn) as c:
        try:
            if project_id is not None:
                require_role(user, project_id, "reviewer", c)
            else:
                role = get_role(user, c)
                if role is None or ROLES[role] < ROLES["reviewer"]:
                    raise PermissionDenied(f"{user!r} may not review")
        except PermissionDenied as e:
            log_action(user, "review_denied", f"{item_type}:{item_id}", str(e), c)
            raise
        c.execute("INSERT INTO reviews(item_type,item_id,decision,comment,reviewer,ts) VALUES(?,?,?,?,?,?)",
                  (item_type, str(item_id), decision, comment, user, _now()))
        table = REVIEW_TABLES.get(item_type)
        if table and c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() \
                and _has_column(c, table, "review_status"):
            c.execute(f"UPDATE {table} SET review_status=? WHERE id=?", (decision, item_id))
        c.commit()
        log_action(user, "review", f"{item_type}:{item_id}", {"decision": decision, "comment": comment}, c)


def list_reviews(item_type: str | None = None, item_id: str | int | None = None,
                 conn: sqlite3.Connection | None = None) -> list[dict]:
    """Review history (oldest first), optionally filtered by item_type and/or item_id."""
    q, args = "SELECT * FROM reviews WHERE 1=1", []
    if item_type:
        q += " AND item_type=?"
        args.append(item_type)
    if item_id is not None:
        q += " AND item_id=?"
        args.append(str(item_id))
    with _use(conn) as c:
        return [dict(r) for r in c.execute(q + " ORDER BY id", args)]
