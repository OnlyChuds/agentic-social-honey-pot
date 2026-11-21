from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from src.models import DecisionRecord, Event, Snapshot, utc_now_iso

MAX_ASSETS_DEFAULT = 300


@dataclass(slots=True)
class AssetRecord:
    asset_id: str
    local_path: str
    sha256: str
    content_type: str
    size_bytes: int
    created_at: str

    @property
    def uri(self) -> str:
        return f"asset://{self.asset_id}"


@dataclass(slots=True)
class RegisteredPage:
    session_id: str
    page_id: str
    url: str
    title: str
    fields: list[str]
    created_at: str


class AssetCapError(RuntimeError):
    """Raised when attempting to store more assets than the platform limit."""


class Store:
    """SQLite (WAL mode) shared memory store for events, decisions, snapshots, assets, and pages."""

    def __init__(self, db_path: str | Path, *, max_assets: int = MAX_ASSETS_DEFAULT) -> None:
        self.db_path = Path(db_path)
        self.max_assets = max_assets
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.execute("PRAGMA foreign_keys=ON;")
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                ts TEXT NOT NULL,
                type TEXT NOT NULL,
                actor TEXT NOT NULL,
                base_version INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
            CREATE INDEX IF NOT EXISTS idx_events_version ON events(base_version);

            CREATE TABLE IF NOT EXISTS decisions (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                trigger TEXT NOT NULL,
                snapshot_version INTEGER NOT NULL,
                record_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_decisions_version ON decisions(snapshot_version);

            CREATE TABLE IF NOT EXISTS snapshots (
                version INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                state TEXT NOT NULL,
                snapshot_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS assets (
                asset_id TEXT PRIMARY KEY,
                local_path TEXT NOT NULL,
                sha256 TEXT NOT NULL UNIQUE,
                content_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_assets_local_path ON assets(local_path);

            CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                page_id TEXT NOT NULL,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                fields_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(session_id, url)
            );
            CREATE INDEX IF NOT EXISTS idx_pages_session ON pages(session_id);
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def journal_mode(self) -> str:
        row = self._conn.execute("PRAGMA journal_mode;").fetchone()
        return str(row[0]).lower() if row else ""

    # --- Event Log (append-only) ---

    def append_event(self, event: Event) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO events (event_id, ts, type, actor, base_version, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.ts,
                event.type,
                event.actor,
                event.base_version,
                json.dumps(event.payload, ensure_ascii=False),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def get_events(
        self,
        *,
        event_type: str | None = None,
        since_version: int | None = None,
        limit: int | None = None,
    ) -> list[Event]:
        clauses: list[str] = []
        params: list[Any] = []
        if event_type is not None:
            clauses.append("type = ?")
            params.append(event_type)
        if since_version is not None:
            clauses.append("base_version >= ?")
            params.append(since_version)

        sql = "SELECT event_id, ts, type, actor, base_version, payload_json FROM events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY seq ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [
            Event(
                event_id=row["event_id"],
                ts=row["ts"],
                type=row["type"],
                actor=row["actor"],
                base_version=row["base_version"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    # --- Decision Log (append-only) ---

    def append_decision(self, record: DecisionRecord) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO decisions (ts, trigger, snapshot_version, record_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                record.ts,
                record.trigger,
                record.snapshot_version,
                record.model_dump_json(),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def get_decisions(self, *, limit: int | None = None) -> list[DecisionRecord]:
        sql = "SELECT record_json FROM decisions ORDER BY seq ASC"
        params: list[Any] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [DecisionRecord.model_validate_json(row["record_json"]) for row in rows]

    # --- Snapshots (versioned) ---

    def save_snapshot(self, snapshot: Snapshot) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO snapshots (version, ts, state, snapshot_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                snapshot.version,
                utc_now_iso(),
                snapshot.run.state,
                snapshot.model_dump_json(),
            ),
        )
        self._conn.commit()

    def get_snapshot(self, version: int) -> Snapshot | None:
        row = self._conn.execute(
            "SELECT snapshot_json FROM snapshots WHERE version = ?",
            (version,),
        ).fetchone()
        if row is None:
            return None
        return Snapshot.model_validate_json(row["snapshot_json"])

    def latest_snapshot(self) -> Snapshot | None:
        row = self._conn.execute(
            "SELECT snapshot_json FROM snapshots ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return Snapshot.model_validate_json(row["snapshot_json"])

    def list_snapshot_versions(self) -> list[int]:
        rows = self._conn.execute("SELECT version FROM snapshots ORDER BY version ASC").fetchall()
        return [int(r["version"]) for r in rows]

    # --- Assets (asset:// ID map + 300-asset cap) ---

    def count_assets(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS cnt FROM assets").fetchone()
        return int(row["cnt"]) if row else 0

    def remaining_asset_budget(self) -> int:
        return max(0, self.max_assets - self.count_assets())

    def get_asset_by_sha256(self, sha256: str) -> AssetRecord | None:
        row = self._conn.execute(
            """
            SELECT asset_id, local_path, sha256, content_type, size_bytes, created_at
            FROM assets WHERE sha256 = ?
            """,
            (sha256,),
        ).fetchone()
        return self._row_to_asset(row) if row else None

    def get_asset_by_path(self, local_path: str | Path) -> AssetRecord | None:
        row = self._conn.execute(
            """
            SELECT asset_id, local_path, sha256, content_type, size_bytes, created_at
            FROM assets WHERE local_path = ?
            """,
            (str(local_path),),
        ).fetchone()
        return self._row_to_asset(row) if row else None

    def get_asset(self, asset_id: str) -> AssetRecord | None:
        row = self._conn.execute(
            """
            SELECT asset_id, local_path, sha256, content_type, size_bytes, created_at
            FROM assets WHERE asset_id = ?
            """,
            (asset_id,),
        ).fetchone()
        return self._row_to_asset(row) if row else None

    def list_assets(self) -> list[AssetRecord]:
        rows = self._conn.execute(
            """
            SELECT asset_id, local_path, sha256, content_type, size_bytes, created_at
            FROM assets ORDER BY created_at ASC
            """
        ).fetchall()
        return [self._row_to_asset(r) for r in rows]

    def record_asset(
        self,
        *,
        asset_id: str,
        local_path: str | Path,
        sha256: str,
        content_type: str,
        size_bytes: int,
    ) -> AssetRecord:
        existing = self.get_asset_by_sha256(sha256)
        if existing is not None:
            return existing
        if self.count_assets() >= self.max_assets:
            raise AssetCapError(
                f"Asset cap reached ({self.max_assets} stored assets); cannot record {local_path}"
            )
        created_at = utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO assets (asset_id, local_path, sha256, content_type, size_bytes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (asset_id, str(local_path), sha256, content_type, size_bytes, created_at),
        )
        self._conn.commit()
        return AssetRecord(
            asset_id=asset_id,
            local_path=str(local_path),
            sha256=sha256,
            content_type=content_type,
            size_bytes=size_bytes,
            created_at=created_at,
        )

    @staticmethod
    def _row_to_asset(row: sqlite3.Row) -> AssetRecord:
        return AssetRecord(
            asset_id=row["asset_id"],
            local_path=row["local_path"],
            sha256=row["sha256"],
            content_type=row["content_type"],
            size_bytes=int(row["size_bytes"]),
            created_at=row["created_at"],
        )

    # --- Registered Landing Pages ---

    def record_page(
        self,
        *,
        session_id: str,
        page_id: str,
        url: str,
        title: str,
        fields: list[str],
    ) -> RegisteredPage:
        created_at = utc_now_iso()
        self._conn.execute(
            """
            INSERT OR REPLACE INTO pages (session_id, page_id, url, title, fields_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                page_id,
                url,
                title,
                json.dumps(fields, ensure_ascii=False),
                created_at,
            ),
        )
        self._conn.commit()
        return RegisteredPage(
            session_id=session_id,
            page_id=page_id,
            url=url,
            title=title,
            fields=list(fields),
            created_at=created_at,
        )

    def get_pages_for_session(self, session_id: str) -> list[RegisteredPage]:
        rows = self._conn.execute(
            """
            SELECT session_id, page_id, url, title, fields_json, created_at
            FROM pages WHERE session_id = ? ORDER BY id ASC
            """,
            (session_id,),
        ).fetchall()
        return [
            RegisteredPage(
                session_id=r["session_id"],
                page_id=r["page_id"],
                url=r["url"],
                title=r["title"],
                fields=json.loads(r["fields_json"]),
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def is_page_registered(self, session_id: str, url: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM pages WHERE session_id = ? AND url = ?",
            (session_id, url),
        ).fetchone()
        return row is not None
