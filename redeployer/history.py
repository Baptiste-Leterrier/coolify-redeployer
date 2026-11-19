"""SQLite history of runs, per-resource changes and run logs, stored in DATA_DIR."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    finished_at REAL,
    trigger TEXT NOT NULL,          -- schedule, api, cli
    dry_run INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,           -- running, success, partial, failed
    summary TEXT                    -- JSON counters
);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    at REAL NOT NULL,
    resource_uuid TEXT NOT NULL,
    resource_name TEXT,
    resource_type TEXT,
    project TEXT,
    environment TEXT,
    component TEXT NOT NULL DEFAULT '',  -- container name inside a service, '' otherwise
    kind TEXT NOT NULL,             -- image, git, none
    subject TEXT,                   -- image reference or git repository/branch
    from_version TEXT,              -- digest or commit before
    to_version TEXT,                -- digest or commit after
    from_label TEXT,                -- human version before (image label, commit message)
    to_label TEXT,
    change TEXT NOT NULL,           -- upgraded, unchanged, first-seen, unknown, failed, skipped
    deploy_status TEXT,             -- queued, finished, failed, cancelled-by-user, timeout, error, dry-run
    deployment_uuid TEXT,
    http_status INTEGER,
    message TEXT
);
CREATE INDEX IF NOT EXISTS changes_run ON changes(run_id);
CREATE INDEX IF NOT EXISTS changes_resource ON changes(resource_uuid, at);
CREATE TABLE IF NOT EXISTS run_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    at REAL NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    fields TEXT
);
CREATE INDEX IF NOT EXISTS run_logs_run ON run_logs(run_id);
CREATE TABLE IF NOT EXISTS image_state (       -- last digest queued per resource/container
    resource_uuid TEXT NOT NULL,
    component TEXT NOT NULL DEFAULT '',
    image TEXT NOT NULL,
    digest TEXT NOT NULL,
    label TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (resource_uuid, component)
);
CREATE TABLE IF NOT EXISTS digest_labels (     -- cache: digest -> image version label
    digest TEXT PRIMARY KEY,
    version TEXT,
    revision TEXT,
    fetched_at REAL NOT NULL
);
"""

CHANGE_COLUMNS = ("resource_uuid", "resource_name", "resource_type", "project", "environment",
                  "component", "kind", "subject", "from_version", "to_version", "from_label",
                  "to_label", "change", "deploy_status", "deployment_uuid", "http_status", "message")


class History:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "history.sqlite3")
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)

    def _exec(self, sql: str, args=()):
        with self._lock:
            return self._db.execute(sql, args)

    def _all(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    # --- runs -----------------------------------------------------------------------------

    def start_run(self, trigger: str, dry_run: bool) -> int:
        cur = self._exec("INSERT INTO runs(started_at, trigger, dry_run, status) VALUES (?,?,?,?)",
                         (time.time(), trigger, int(dry_run), "running"))
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, summary: dict) -> None:
        self._exec("UPDATE runs SET finished_at=?, status=?, summary=? WHERE id=?",
                   (time.time(), status, json.dumps(summary), run_id))

    def mark_interrupted(self) -> int:
        """Runs left 'running' by a container stop are closed as 'interrupted' at startup."""
        cur = self._exec("UPDATE runs SET status='interrupted', finished_at=? WHERE status='running'",
                         (time.time(),))
        return cur.rowcount

    def runs(self, limit: int = 20) -> list[dict]:
        rows = self._all("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["summary"] = json.loads(r["summary"]) if r.get("summary") else {}
        return rows

    def run(self, run_id: int) -> dict | None:
        rows = self._all("SELECT * FROM runs WHERE id=?", (run_id,))
        if not rows:
            return None
        r = rows[0]
        r["summary"] = json.loads(r["summary"]) if r.get("summary") else {}
        r["changes"] = self._all("SELECT * FROM changes WHERE run_id=? ORDER BY id", (run_id,))
        return r

    # --- changes --------------------------------------------------------------------------

    def add_change(self, run_id: int, **values) -> None:
        cols = [c for c in CHANGE_COLUMNS if c in values]
        self._exec(
            f"INSERT INTO changes(run_id, at, {', '.join(cols)}) VALUES (?, ?, {', '.join('?' for _ in cols)})",
            (run_id, time.time(), *(values[c] for c in cols)))

    def changes(self, resource: str | None = None, change: str | None = None, limit: int = 100,
                include_dry_run: bool = False) -> list[dict]:
        where, args = [], []
        if resource:
            where.append("(c.resource_uuid = ? OR lower(c.resource_name) = lower(?))")
            args += [resource, resource]
        if change:
            where.append("c.change = ?")
            args.append(change)
        if not include_dry_run:
            where.append("r.dry_run = 0")
        sql = ("SELECT c.*, r.trigger FROM changes c JOIN runs r ON r.id = c.run_id"
               + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY c.id DESC LIMIT ?")
        return self._all(sql, (*args, limit))

    # --- image state ----------------------------------------------------------------------

    def image_state(self, resource_uuid: str, component: str) -> dict | None:
        rows = self._all("SELECT * FROM image_state WHERE resource_uuid=? AND component=?",
                         (resource_uuid, component))
        return rows[0] if rows else None

    def set_image_state(self, resource_uuid: str, component: str, image: str, digest: str,
                        label: str | None) -> None:
        self._exec("INSERT INTO image_state(resource_uuid, component, image, digest, label, updated_at) "
                   "VALUES (?,?,?,?,?,?) ON CONFLICT(resource_uuid, component) DO UPDATE SET "
                   "image=excluded.image, digest=excluded.digest, label=excluded.label, "
                   "updated_at=excluded.updated_at",
                   (resource_uuid, component, image, digest, label, time.time()))

    def digest_label(self, digest: str) -> dict | None:
        rows = self._all("SELECT * FROM digest_labels WHERE digest=?", (digest,))
        return rows[0] if rows else None

    def set_digest_label(self, digest: str, version: str | None, revision: str | None) -> None:
        self._exec("INSERT OR REPLACE INTO digest_labels(digest, version, revision, fetched_at) "
                   "VALUES (?,?,?,?)", (digest, version, revision, time.time()))

    # --- logs -----------------------------------------------------------------------------

    def add_log(self, run_id, at: float, level: str, message: str, fields: dict) -> None:
        self._exec("INSERT INTO run_logs(run_id, at, level, message, fields) VALUES (?,?,?,?,?)",
                   (run_id, at, level, message, json.dumps(fields, default=str) if fields else None))

    def logs(self, run_id: int) -> list[dict]:
        rows = self._all("SELECT at, level, message, fields FROM run_logs WHERE run_id=? ORDER BY id",
                         (run_id,))
        for r in rows:
            r["fields"] = json.loads(r["fields"]) if r.get("fields") else {}
        return rows

    # --- maintenance ----------------------------------------------------------------------

    def prune(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = time.time() - retention_days * 86400
        cur = self._exec("DELETE FROM runs WHERE started_at < ? AND status != 'running'", (cutoff,))
        return cur.rowcount
