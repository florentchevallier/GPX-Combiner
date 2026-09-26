"""
GPX Combiner mobile — SQLite data model.

Single table: one row per Strava athlete, identified by their Strava
athlete_id (no password: authentication goes entirely through Strava's own
OAuth flow — see app.py).

Designed for a small group (5-15 people): a single SQLite file, zero
database server to manage. If the app grows a lot later, migrating to
PostgreSQL would only change the connection, not the schema.
"""

import sqlite3
from contextlib import contextmanager

DB_PATH = "gpx_combiner_mobile.db"


def init_db(db_path: str = DB_PATH):
    """Creates the users table if it doesn't exist yet. Call once at app
    startup (see app.py)."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                athlete_id      INTEGER PRIMARY KEY,
                firstname       TEXT,
                lastname        TEXT,
                access_token    TEXT NOT NULL,
                refresh_token   TEXT NOT NULL,
                expires_at      INTEGER NOT NULL,  -- Unix timestamp
                created_at      INTEGER NOT NULL DEFAULT (strftime('%s','now'))
            )
            """
        )
        conn.commit()


@contextmanager
def get_db(db_path: str = DB_PATH):
    """Small context manager to open/close the connection cleanly."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def upsert_user(athlete_id: int, firstname: str, lastname: str,
                 access_token: str, refresh_token: str, expires_at: int,
                 db_path: str = DB_PATH):
    """Stores or updates a user's tokens after OAuth authorization (first
    login, or a token refresh)."""
    with get_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO users (athlete_id, firstname, lastname,
                                access_token, refresh_token, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(athlete_id) DO UPDATE SET
                firstname=excluded.firstname,
                lastname=excluded.lastname,
                access_token=excluded.access_token,
                refresh_token=excluded.refresh_token,
                expires_at=excluded.expires_at
            """,
            (athlete_id, firstname, lastname, access_token, refresh_token, expires_at),
        )
        conn.commit()


def get_user(athlete_id: int, db_path: str = DB_PATH):
    """Returns the user row (or None) for a given athlete_id."""
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE athlete_id = ?", (athlete_id,)
        ).fetchone()
        return dict(row) if row else None
