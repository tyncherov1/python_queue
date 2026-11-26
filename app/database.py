from __future__ import annotations

import csv
import io
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from app.config import Settings
from app.models import QueueEntry, Student

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    telegram_id INTEGER UNIQUE,
    username TEXT,
    is_admin INTEGER NOT NULL DEFAULT 0,
    last_joined_at REAL,
    last_served_at REAL,
    last_left_at REAL,
    visits_count INTEGER NOT NULL DEFAULT 0,
    active_in_roster INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_students_username ON students(username);
CREATE INDEX IF NOT EXISTS idx_students_active ON students(active_in_roster);

CREATE TABLE IF NOT EXISTS queue_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER REFERENCES students(id) ON DELETE SET NULL,
    display_name TEXT NOT NULL,
    telegram_id INTEGER,
    status TEXT NOT NULL,
    position INTEGER,
    joined_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    added_by_admin INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_queue_status_pos ON queue_entries(status, position);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _normalize_username(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = raw.strip().lstrip("@").lower()
    return cleaned or None


def _parse_optional_int(raw: str | None) -> int | None:
    if not raw:
        return None
    cleaned = raw.strip()
    if cleaned and cleaned.lstrip("-").isdigit():
        return int(cleaned)
    return None


def _read_csv_rows(csv_path: Path) -> list[dict[str, str]]:
    raw_bytes = csv_path.read_bytes()
    text: str | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            text = raw_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError(f"Unable to decode {csv_path} as UTF-8 or CP1251")

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    header_line = lines[0]
    delimiter = ";" if header_line.count(";") > header_line.count(",") else ","

    reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    rows: list[dict[str, str]] = []
    for raw_row in reader:
        normalized = {
            (k or "").strip().lower(): (v or "").strip()
            for k, v in raw_row.items()
            if k is not None
        }
        rows.append(normalized)
    return rows


def _pick(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        if key in row and row[key]:
            return row[key]
    return ""


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def connect(self) -> None:
        self.settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.settings.db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA_SQL)
        await self._ensure_migrations()
        await self._init_default_state()
        await self._conn.commit()

    async def _ensure_migrations(self) -> None:
        """Safely upgrade existing SQLite databases created before schema additions."""
        cur = await self.conn.execute("PRAGMA table_info(students)")
        columns = {row["name"] for row in await cur.fetchall()}
        if "last_joined_at" not in columns:
            await self.conn.execute("ALTER TABLE students ADD COLUMN last_joined_at REAL")
            logger.info("Migrated students table: added last_joined_at column")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _init_default_state(self) -> None:
        defaults = {
            "seminar_title": self.settings.seminar_title,
            "timer_duration_sec": str(self.settings.default_timer_seconds),
            "timer_remaining_sec": str(float(self.settings.default_timer_seconds)),
            "timer_status": "idle",
            "timer_started_at": "",
            "timer_epoch": "1",
            "rejoin_cooldown_minutes": str(self.settings.rejoin_cooldown_minutes),
            "cancel_cooldown_minutes": str(self.settings.cancel_cooldown_minutes),
            "max_active_slots": str(self.settings.max_active_slots),
            "queue_open": "1",
            "auto_start_timer": "0",
            "board_visible_queue": str(self.settings.board_visible_queue),
        }
        for key, value in defaults.items():
            await self.conn.execute(
                "INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)",
                (key, value),
            )

    # ------------------------------------------------------------------
    # App State Helpers
    # ------------------------------------------------------------------
    async def get_all_state(self) -> dict[str, str]:
        cursor = await self.conn.execute("SELECT key, value FROM app_state")
        rows = await cursor.fetchall()
        return {row["key"]: row["value"] for row in rows}

    async def get_state(self, key: str, default: str = "") -> str:
        cursor = await self.conn.execute(
            "SELECT value FROM app_state WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row["value"] if row else default

    async def set_state(self, updates: dict[str, Any], commit: bool = True) -> None:
        for key, value in updates.items():
            await self.conn.execute(
                """
                INSERT INTO app_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, str(value)),
            )
        if commit:
            await self.conn.commit()

    # ------------------------------------------------------------------
    # Students & Whitelist Sync
    # ------------------------------------------------------------------
    async def sync_students_from_csv(self, csv_path: Path | None = None) -> int:
        path = csv_path or self.settings.students_csv
        if not path.exists():
            logger.warning("Students CSV file not found at %s", path)
            return 0

        rows = _read_csv_rows(path)
        seen_ids: set[int] = set()
        loaded_count = 0

        for row in rows:
            full_name = _pick(row, "full_name", "name", "фио", "имя", "ученик", "студент")
            if not full_name:
                continue

            tg_id = _parse_optional_int(
                _pick(row, "telegram_id", "tg_id", "user_id", "телеграм_id")
            )
            username = _normalize_username(
                _pick(row, "username", "tg_username", "ник", "логин")
            )
            role = _pick(row, "role", "роль").lower()
            is_admin = 1 if role in {"admin", "админ", "преподаватель", "teacher"} else 0
            if tg_id is not None and tg_id in self.settings.admin_ids:
                is_admin = 1

            existing = None
            if tg_id is not None:
                cur = await self.conn.execute(
                    "SELECT * FROM students WHERE telegram_id = ?", (tg_id,)
                )
                existing = await cur.fetchone()
            if existing is None and username is not None:
                cur = await self.conn.execute(
                    "SELECT * FROM students WHERE lower(username) = ?", (username,)
                )
                existing = await cur.fetchone()
            if existing is None:
                cur = await self.conn.execute(
                    "SELECT * FROM students WHERE lower(full_name) = ?",
                    (full_name.lower(),),
                )
                existing = await cur.fetchone()

            if existing is not None:
                resolved_tg_id = tg_id if tg_id is not None else existing["telegram_id"]
                resolved_username = (
                    username if username is not None else existing["username"]
                )
                if resolved_tg_id is not None and resolved_tg_id in self.settings.admin_ids:
                    is_admin = 1
                await self.conn.execute(
                    """
                    UPDATE students
                    SET full_name = ?,
                        telegram_id = ?,
                        username = ?,
                        is_admin = ?,
                        active_in_roster = 1
                    WHERE id = ?
                    """,
                    (
                        full_name,
                        resolved_tg_id,
                        resolved_username,
                        is_admin,
                        existing["id"],
                    ),
                )
                seen_ids.add(existing["id"])
            else:
                cur = await self.conn.execute(
                    """
                    INSERT INTO students (full_name, telegram_id, username, is_admin, active_in_roster)
                    VALUES (?, ?, ?, ?, 1)
                    """,
                    (full_name, tg_id, username, is_admin),
                )
                seen_ids.add(int(cur.lastrowid))

            loaded_count += 1

        # Deactivate roster entries removed from CSV (keep env ADMIN_IDS active)
        cur = await self.conn.execute("SELECT id, telegram_id FROM students")
        for row in await cur.fetchall():
            if row["id"] not in seen_ids:
                if row["telegram_id"] in self.settings.admin_ids:
                    await self.conn.execute(
                        "UPDATE students SET is_admin = 1, active_in_roster = 1 WHERE id = ?",
                        (row["id"],),
                    )
                else:
                    await self.conn.execute(
                        "UPDATE students SET active_in_roster = 0 WHERE id = ?",
                        (row["id"],),
                    )

        await self.conn.commit()
        logger.info("Synced %d students from %s", loaded_count, path)
        return loaded_count

    @staticmethod
    def _row_to_student(row: aiosqlite.Row) -> Student:
        keys = row.keys()
        last_joined = row["last_joined_at"] if "last_joined_at" in keys else None
        return Student(
            id=int(row["id"]),
            full_name=str(row["full_name"]),
            telegram_id=int(row["telegram_id"]) if row["telegram_id"] is not None else None,
            username=str(row["username"]) if row["username"] is not None else None,
            is_admin=bool(row["is_admin"]),
            last_joined_at=float(last_joined) if last_joined is not None else None,
            last_served_at=float(row["last_served_at"]) if row["last_served_at"] is not None else None,
            last_left_at=float(row["last_left_at"]) if row["last_left_at"] is not None else None,
            visits_count=int(row["visits_count"]),
            active_in_roster=bool(row["active_in_roster"]),
        )

    async def resolve_telegram_user(
        self,
        telegram_id: int,
        username: str | None,
        fallback_full_name: str = "Администратор",
    ) -> Student | None:
        norm_username = _normalize_username(username)

        # 1. Exact telegram_id match in active roster
        cur = await self.conn.execute(
            "SELECT * FROM students WHERE telegram_id = ? AND active_in_roster = 1",
            (telegram_id,),
        )
        row = await cur.fetchone()
        if row is not None:
            is_admin = bool(row["is_admin"]) or (telegram_id in self.settings.admin_ids)
            if norm_username and row["username"] != norm_username or is_admin != bool(row["is_admin"]):
                await self.conn.execute(
                    "UPDATE students SET username = ?, is_admin = ? WHERE id = ?",
                    (norm_username or row["username"], int(is_admin), row["id"]),
                )
                await self.conn.commit()
                cur = await self.conn.execute("SELECT * FROM students WHERE id = ?", (row["id"],))
                row = await cur.fetchone()
            return self._row_to_student(row)

        # 2. Match by username in active roster (bind telegram_id automatically!)
        if norm_username:
            cur = await self.conn.execute(
                """
                SELECT * FROM students
                WHERE lower(username) = ?
                  AND active_in_roster = 1
                  AND (telegram_id IS NULL OR telegram_id = ?)
                """,
                (norm_username, telegram_id),
            )
            row = await cur.fetchone()
            if row is not None:
                is_admin = bool(row["is_admin"]) or (telegram_id in self.settings.admin_ids)
                await self.conn.execute(
                    "UPDATE students SET telegram_id = ?, is_admin = ? WHERE id = ?",
                    (telegram_id, int(is_admin), row["id"]),
                )
                # Backfill telegram_id on active/waiting queue entries if added by admin earlier
                await self.conn.execute(
                    """
                    UPDATE queue_entries
                    SET telegram_id = ?
                    WHERE student_id = ? AND status IN ('waiting', 'active') AND telegram_id IS NULL
                    """,
                    (telegram_id, row["id"]),
                )
                await self.conn.commit()
                cur = await self.conn.execute("SELECT * FROM students WHERE id = ?", (row["id"],))
                row = await cur.fetchone()
                return self._row_to_student(row)

        # 3. Admin configured via ADMIN_IDS in .env (even if not in students.csv)
        if telegram_id in self.settings.admin_ids:
            cur = await self.conn.execute(
                "SELECT * FROM students WHERE telegram_id = ?", (telegram_id,)
            )
            existing = await cur.fetchone()
            if existing is not None:
                await self.conn.execute(
                    "UPDATE students SET is_admin = 1, active_in_roster = 1, username = COALESCE(?, username) WHERE id = ?",
                    (norm_username, existing["id"]),
                )
                await self.conn.commit()
                cur = await self.conn.execute("SELECT * FROM students WHERE id = ?", (existing["id"],))
                return self._row_to_student(await cur.fetchone())

            cur = await self.conn.execute(
                """
                INSERT INTO students (full_name, telegram_id, username, is_admin, active_in_roster)
                VALUES (?, ?, ?, 1, 1)
                """,
                (fallback_full_name, telegram_id, norm_username),
            )
            await self.conn.commit()
            cur = await self.conn.execute("SELECT * FROM students WHERE id = ?", (cur.lastrowid,))
            return self._row_to_student(await cur.fetchone())

        return None

    async def get_student_by_id(self, student_id: int) -> Student | None:
        cur = await self.conn.execute("SELECT * FROM students WHERE id = ?", (student_id,))
        row = await cur.fetchone()
        return self._row_to_student(row) if row else None

    async def list_roster_students(self) -> list[Student]:
        cur = await self.conn.execute(
            "SELECT * FROM students WHERE active_in_roster = 1 ORDER BY full_name COLLATE NOCASE ASC"
        )
        return [self._row_to_student(r) for r in await cur.fetchall()]

    async def find_students_by_query(self, query: str) -> list[Student]:
        q = f"%{query.strip().lower()}%"
        cur = await self.conn.execute(
            """
            SELECT * FROM students
            WHERE active_in_roster = 1
              AND (lower(full_name) LIKE ? OR lower(COALESCE(username, '')) LIKE ?)
            ORDER BY full_name COLLATE NOCASE ASC
            LIMIT 10
            """,
            (q, q),
        )
        return [self._row_to_student(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------------
    # Queue Entry Queries
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_entry(row: aiosqlite.Row) -> QueueEntry:
        return QueueEntry(
            id=int(row["id"]),
            student_id=int(row["student_id"]) if row["student_id"] is not None else None,
            display_name=str(row["display_name"]),
            telegram_id=int(row["telegram_id"]) if row["telegram_id"] is not None else None,
            status=row["status"],
            position=int(row["position"]) if row["position"] is not None else None,
            joined_at=float(row["joined_at"]),
            started_at=float(row["started_at"]) if row["started_at"] is not None else None,
            finished_at=float(row["finished_at"]) if row["finished_at"] is not None else None,
            added_by_admin=bool(row["added_by_admin"]),
            visits_count=int(row["visits_count"]) if "visits_count" in row.keys() and row["visits_count"] is not None else 0,
        )

    async def get_active_entry(self) -> QueueEntry | None:
        cur = await self.conn.execute(
            """
            SELECT q.*, COALESCE(s.visits_count, 0) AS visits_count
            FROM queue_entries q
            LEFT JOIN students s ON s.id = q.student_id
            WHERE q.status = 'active'
            ORDER BY q.started_at DESC, q.id DESC
            LIMIT 1
            """
        )
        row = await cur.fetchone()
        return self._row_to_entry(row) if row else None

    async def get_waiting_entries(self) -> list[QueueEntry]:
        cur = await self.conn.execute(
            """
            SELECT q.*, COALESCE(s.visits_count, 0) AS visits_count
            FROM queue_entries q
            LEFT JOIN students s ON s.id = q.student_id
            WHERE q.status = 'waiting'
            ORDER BY q.position ASC, q.joined_at ASC, q.id ASC
            """
        )
        return [self._row_to_entry(r) for r in await cur.fetchall()]

    async def get_entry_by_id(self, entry_id: int) -> QueueEntry | None:
        cur = await self.conn.execute(
            """
            SELECT q.*, COALESCE(s.visits_count, 0) AS visits_count
            FROM queue_entries q
            LEFT JOIN students s ON s.id = q.student_id
            WHERE q.id = ?
            """,
            (entry_id,),
        )
        row = await cur.fetchone()
        return self._row_to_entry(row) if row else None

    async def get_student_open_entries(self, student_id: int) -> list[QueueEntry]:
        cur = await self.conn.execute(
            """
            SELECT q.*, COALESCE(s.visits_count, 0) AS visits_count
            FROM queue_entries q
            LEFT JOIN students s ON s.id = q.student_id
            WHERE q.student_id = ? AND q.status IN ('waiting', 'active')
            ORDER BY CASE WHEN q.status = 'active' THEN 0 ELSE 1 END, q.position ASC, q.id ASC
            """,
            (student_id,),
        )
        return [self._row_to_entry(r) for r in await cur.fetchall()]

    async def get_student_waiting_entries(self, student_id: int) -> list[QueueEntry]:
        cur = await self.conn.execute(
            """
            SELECT q.*, COALESCE(s.visits_count, 0) AS visits_count
            FROM queue_entries q
            LEFT JOIN students s ON s.id = q.student_id
            WHERE q.student_id = ? AND q.status = 'waiting'
            ORDER BY q.position ASC, q.id ASC
            """,
            (student_id,),
        )
        return [self._row_to_entry(r) for r in await cur.fetchall()]

    async def get_student_open_entry(self, student_id: int) -> QueueEntry | None:
        entries = await self.get_student_open_entries(student_id)
        return entries[0] if entries else None

    async def get_last_self_joined_at(self, student_id: int) -> float | None:
        cur = await self.conn.execute(
            """
            SELECT MAX(joined_at) AS last_ts
            FROM queue_entries
            WHERE student_id = ?
              AND added_by_admin = 0
              AND status IN ('waiting', 'active', 'done')
            """,
            (student_id,),
        )
        row = await cur.fetchone()
        return float(row["last_ts"]) if row and row["last_ts"] is not None else None

    async def renumber_waiting(self, ordered_ids: list[int] | None = None) -> None:
        if ordered_ids is None:
            cur = await self.conn.execute(
                """
                SELECT id FROM queue_entries
                WHERE status = 'waiting'
                ORDER BY position ASC, joined_at ASC, id ASC
                """
            )
            ordered_ids = [int(r["id"]) for r in await cur.fetchall()]

        for idx, entry_id in enumerate(ordered_ids, start=1):
            await self.conn.execute(
                "UPDATE queue_entries SET position = ? WHERE id = ?",
                (idx, entry_id),
            )

    async def get_stats(self) -> dict[str, Any]:
        cur = await self.conn.execute(
            """
            SELECT
                COUNT(*) AS served_count,
                AVG(CASE WHEN started_at IS NOT NULL AND finished_at IS NOT NULL
                    THEN finished_at - started_at END) AS avg_duration_sec
            FROM queue_entries
            WHERE status = 'done'
            """
        )
        row = await cur.fetchone()
        served_count = int(row["served_count"] or 0)
        avg_duration = float(row["avg_duration_sec"]) if row["avg_duration_sec"] is not None else 0.0

        cur_recent = await self.conn.execute(
            """
            SELECT display_name, started_at, finished_at
            FROM queue_entries
            WHERE status = 'done'
            ORDER BY finished_at DESC, id DESC
            LIMIT 5
            """
        )
        recent_done = [
            {
                "display_name": r["display_name"],
                "duration_sec": round(float(r["finished_at"] - r["started_at"]))
                if r["started_at"] and r["finished_at"]
                else None,
            }
            for r in await cur_recent.fetchall()
        ]

        cur_roster = await self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM students WHERE active_in_roster = 1"
        )
        roster_count = int((await cur_roster.fetchone())["cnt"])

        return {
            "served_count": served_count,
            "avg_duration_sec": round(avg_duration),
            "recent_done": recent_done,
            "roster_count": roster_count,
        }

    async def get_history_entries(self) -> list[QueueEntry]:
        cur = await self.conn.execute(
            """
            SELECT q.*, COALESCE(s.visits_count, 0) AS visits_count
            FROM queue_entries q
            LEFT JOIN students s ON s.id = q.student_id
            ORDER BY q.joined_at ASC, q.id ASC
            """
        )
        return [self._row_to_entry(r) for r in await cur.fetchall()]

    async def get_last_done_entry(self) -> QueueEntry | None:
        cur = await self.conn.execute(
            """
            SELECT q.*, COALESCE(s.visits_count, 0) AS visits_count
            FROM queue_entries q
            LEFT JOIN students s ON s.id = q.student_id
            WHERE q.status IN ('done', 'skipped')
            ORDER BY q.finished_at DESC, q.id DESC
            LIMIT 1
            """
        )
        row = await cur.fetchone()
        return self._row_to_entry(row) if row else None

    async def list_notifiable_telegram_ids(self, scope: str = "queue") -> list[int]:
        if scope == "all":
            cur = await self.conn.execute(
                """
                SELECT DISTINCT telegram_id FROM students
                WHERE active_in_roster = 1 AND telegram_id IS NOT NULL
                """
            )
        else:
            cur = await self.conn.execute(
                """
                SELECT DISTINCT telegram_id FROM queue_entries
                WHERE status IN ('waiting', 'active') AND telegram_id IS NOT NULL
                """
            )
        return [int(r["telegram_id"]) for r in await cur.fetchall()]
