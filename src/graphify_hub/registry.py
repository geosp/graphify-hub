"""The project registry: one SQLite table on the data volume."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PENDING, BUILDING, READY, FAILED = "pending", "building", "ready", "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    name        TEXT PRIMARY KEY,
    repo_url    TEXT NOT NULL,
    branch      TEXT NOT NULL,
    status      TEXT NOT NULL,
    head_sha    TEXT,
    built_sha   TEXT,
    built_at    TEXT,
    last_error  TEXT,
    node_count  INTEGER,
    edge_count  INTEGER,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
)
"""


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class Project:
    name: str
    repo_url: str
    branch: str
    status: str
    head_sha: str | None
    built_sha: str | None
    built_at: str | None
    last_error: str | None
    node_count: int | None
    edge_count: int | None
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Registry:
    """Thread-safe CRUD over the projects table."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def add(self, name: str, repo_url: str, branch: str) -> Project:
        now = _now()
        with self._lock:
            self._db.execute(
                "INSERT INTO projects (name, repo_url, branch, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, repo_url, branch, PENDING, now, now),
            )
        project = self.get(name)
        if project is None:  # pragma: no cover - just inserted
            raise RuntimeError(f"project {name} vanished after insert")
        return project

    def get(self, name: str) -> Project | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
        return Project(**dict(row)) if row else None

    def all(self) -> list[Project]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM projects ORDER BY name").fetchall()
        return [Project(**dict(r)) for r in rows]

    _UPDATABLE = frozenset(
        {"status", "head_sha", "built_sha", "built_at", "last_error", "node_count", "edge_count"}
    )

    def update(self, name: str, **fields: Any) -> None:
        unknown = set(fields) - self._UPDATABLE
        if unknown:
            raise ValueError(f"cannot update {sorted(unknown)}")
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._db.execute(
                f"UPDATE projects SET {cols} WHERE name = ?",  # noqa: S608 - keys are allowlisted
                (*fields.values(), name),
            )

    def remove(self, name: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM projects WHERE name = ?", (name,))
        return cur.rowcount > 0

    def reset_interrupted(self) -> list[str]:
        """Mark builds that were running when the hub stopped as pending again."""
        with self._lock:
            rows = self._db.execute(
                "SELECT name FROM projects WHERE status IN (?, ?)", (BUILDING, PENDING)
            ).fetchall()
            self._db.execute(
                "UPDATE projects SET status = ?, updated_at = ? WHERE status = ?",
                (PENDING, _now(), BUILDING),
            )
        return [r["name"] for r in rows]
