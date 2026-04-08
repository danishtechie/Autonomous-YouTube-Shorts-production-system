"""
database.py — SQLite persistence layer.
Tracks topics, generated videos, upload history, and analytics.
"""

import sqlite3
import json
from datetime import datetime, date
from pathlib import Path
from typing import Optional, List, Dict, Any
from loguru import logger
import config


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create all tables if they don't exist."""
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS topics (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT NOT NULL,
            title       TEXT NOT NULL,
            source      TEXT,          -- reddit | google_trends | youtube
            score       REAL,          -- virality score 0-100
            niche       TEXT,
            used        INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS scripts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id    INTEGER REFERENCES topics(id),
            title       TEXT NOT NULL,
            hook        TEXT,
            body        TEXT,
            cta         TEXT,
            full_text   TEXT,
            hook_score  REAL,
            retention_score REAL,
            emotion_score   REAL,
            total_score     REAL,
            word_count  INTEGER,
            est_duration REAL,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS videos (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            script_id    INTEGER REFERENCES scripts(id),
            audio_path   TEXT,
            video_path   TEXT,
            subtitle_path TEXT,
            final_path   TEXT,
            duration     REAL,
            status       TEXT DEFAULT 'pending',  -- pending|rendered|uploaded|failed
            error        TEXT,
            created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
            rendered_at  TEXT,
            uploaded_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS uploads (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id     INTEGER REFERENCES videos(id),
            youtube_id   TEXT UNIQUE,
            title        TEXT,
            description  TEXT,
            tags         TEXT,         -- JSON array
            upload_date  TEXT,
            views        INTEGER DEFAULT 0,
            likes        INTEGER DEFAULT 0,
            status       TEXT DEFAULT 'public',
            created_at   TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS run_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date    TEXT NOT NULL,
            step        TEXT,
            status      TEXT,          -- success|failure
            message     TEXT,
            duration_s  REAL,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
    logger.info("Database initialized")


# ── Topic helpers ─────────────────────────────────────────────────────────────

def save_topics(topics: List[Dict]) -> List[int]:
    today = date.today().isoformat()
    ids = []
    with get_conn() as conn:
        for t in topics:
            cur = conn.execute(
                "INSERT INTO topics (date, title, source, score, niche) VALUES (?,?,?,?,?)",
                (today, t["title"], t.get("source"), t.get("score", 50), config.NICHE)
            )
            ids.append(cur.lastrowid)
    return ids


def get_unused_topic() -> Optional[Dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM topics WHERE used=0 AND date=? ORDER BY score DESC LIMIT 1",
            (date.today().isoformat(),)
        ).fetchone()
    return dict(row) if row else None


def mark_topic_used(topic_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE topics SET used=1 WHERE id=?", (topic_id,))


# ── Script helpers ────────────────────────────────────────────────────────────

def save_script(data: Dict) -> int:
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO scripts
              (topic_id, title, hook, body, cta, full_text,
               hook_score, retention_score, emotion_score, total_score,
               word_count, est_duration)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            data.get("topic_id"), data["title"], data["hook"],
            data["body"], data["cta"], data["full_text"],
            data.get("hook_score", 0), data.get("retention_score", 0),
            data.get("emotion_score", 0), data.get("total_score", 0),
            data.get("word_count", 0), data.get("est_duration", 0),
        ))
    return cur.lastrowid


# ── Video helpers ─────────────────────────────────────────────────────────────

def create_video_record(script_id: int) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO videos (script_id, status) VALUES (?, 'pending')",
            (script_id,)
        )
    return cur.lastrowid


def update_video(video_id: int, **kwargs):
    if not kwargs:
        return
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [video_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE videos SET {sets} WHERE id=?", vals)


def save_upload(video_id: int, data: Dict) -> int:
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO uploads
              (video_id, youtube_id, title, description, tags, upload_date)
            VALUES (?,?,?,?,?,?)
        """, (
            video_id, data["youtube_id"], data["title"],
            data["description"], json.dumps(data.get("tags", [])),
            date.today().isoformat()
        ))
    return cur.lastrowid


def log_run(run_date: str, step: str, status: str,
            message: str = "", duration_s: float = 0):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO run_log (run_date, step, status, message, duration_s) VALUES (?,?,?,?,?)",
            (run_date, step, status, message, duration_s)
        )


def videos_uploaded_today() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM uploads WHERE upload_date=?",
            (date.today().isoformat(),)
        ).fetchone()
    return row["cnt"] if row else 0


# Initialize on import
init_db()
