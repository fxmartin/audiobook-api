# ABOUTME: SQLite-backed async job store for audiobook conversion tasks
# ABOUTME: Tracks job status, progress (chapters/chunks), and error state
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import aiosqlite

DB_PATH = "data/jobs.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT DEFAULT 'queued',
    filename TEXT,
    format TEXT,
    voice TEXT,
    language TEXT,
    use_clone INTEGER DEFAULT 0,
    chapters_total INTEGER DEFAULT 0,
    chapters_done INTEGER DEFAULT 0,
    chunks_current_total INTEGER DEFAULT 0,
    chunks_current_done INTEGER DEFAULT 0,
    error TEXT,
    created_at TEXT,
    updated_at TEXT,
    completed_at TEXT
);
"""


async def init_db():
    """Create tables if they don't exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


def new_job_id() -> str:
    return uuid.uuid4().hex[:12]


async def create_job(
    job_id: str,
    filename: str,
    fmt: str,
    voice: str,
    language: str,
    use_clone: bool,
) -> dict:
    """Insert a new job and return its row as dict."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO jobs (id, status, filename, format, voice, language, use_clone, created_at, updated_at)
               VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, filename, fmt, voice, language, int(use_clone), now, now),
        )
        await db.commit()
    return await get_job(job_id)


async def get_job(job_id: str) -> dict | None:
    """Fetch a single job by ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def list_jobs() -> list[dict]:
    """List all jobs, newest first."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs ORDER BY created_at DESC") as cursor:
            return [dict(row) async for row in cursor]


async def update_status(job_id: str, status: str, error: str | None = None):
    """Update job status and optionally set error."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        if error:
            await db.execute(
                "UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?",
                (status, error, now, job_id),
            )
        elif status == "completed":
            await db.execute(
                "UPDATE jobs SET status=?, updated_at=?, completed_at=? WHERE id=?",
                (status, now, now, job_id),
            )
        else:
            await db.execute(
                "UPDATE jobs SET status=?, updated_at=? WHERE id=?",
                (status, now, job_id),
            )
        await db.commit()


async def update_chapters_total(job_id: str, total: int):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE jobs SET chapters_total=?, updated_at=? WHERE id=?",
            (total, now, job_id),
        )
        await db.commit()


async def update_chapter_progress(job_id: str, done: int):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE jobs SET chapters_done=?, chunks_current_done=0, chunks_current_total=0, updated_at=? WHERE id=?",
            (done, now, job_id),
        )
        await db.commit()


async def update_chunk_progress(job_id: str, done: int, total: int):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE jobs SET chunks_current_done=?, chunks_current_total=?, updated_at=? WHERE id=?",
            (done, total, now, job_id),
        )
        await db.commit()


async def delete_job(job_id: str):
    """Delete a job record."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        await db.commit()
